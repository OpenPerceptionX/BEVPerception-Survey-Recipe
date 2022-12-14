# Copyright (c) 2022 OpenPerceptionX. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


import numpy as np
import torch
import torch.nn as nn
from torch.nn.init import normal_
from torchvision.transforms.functional import rotate
from mmcv.cnn import xavier_init
from mmcv.cnn.bricks.transformer import build_transformer_layer_sequence
from mmcv.runner.base_module import BaseModule
from mmcv.utils import ext_loader

ext_module = ext_loader.load_ext('_ext', ['ms_deform_attn_backward', 'ms_deform_attn_forward'])
from mmdet.models.utils.builder import TRANSFORMER
from ..attns.detr3d_cross_attention import Detr3DCrossAtten
from ..attns.multi_scale_deformable_attn import CustomMultiScaleDeformableAttention
from ..attns.multi_scale_deformable_attn_V2 import CustomMultiScaleDeformableAttentionV2
from ..attns.multi_scale_deformable_attn_V4 import CustomMultiScaleDeformableAttentionV4
from ..attns.multi_scale_deformable_attn_3d import MultiScaleDeformableAttention3D


def inverse_sigmoid(x, eps=1e-5):
    """Inverse function of sigmoid.
    Args:
        x (Tensor): The tensor to do the
            inverse.
        eps (float): EPS avoid numerical
            overflow. Defaults 1e-5.
    Returns:
        Tensor: The x has passed the inverse
            function of sigmoid, has same
            shape with input.
    """
    x = x.clamp(min=0, max=1)
    x1 = x.clamp(min=eps)
    x2 = (1 - x).clamp(min=eps)
    return torch.log(x1 / x2)


@TRANSFORMER.register_module()
class BEVTransformer(BaseModule):
    """Implements the Detr3D transformer.
    Args:
        as_two_stage (bool): Generate query from encoder features.
            Default: False.
        num_feature_levels (int): Number of feature maps from FPN:
            Default: 4.
        two_stage_num_proposals (int): Number of proposals when set
            `as_two_stage` as True. Default: 300.
    """

    def __init__(
            self,
            num_feature_levels=4,
            num_cams=6,
            two_stage_num_proposals=300,
            encoder=None,
            decoder=None,
            Z=8,  # the range the Z-axis
            D=4,  # the number of sample points on Z-axis
            embed_dims=256,
            rotate_prev_bev=True,
            only_encoder=False,
            use_can_bus=True,
            can_bus_norm=True,
            use_shift=True,
            use_cams_embeds=True,
            **kwargs):
        super(BEVTransformer, self).__init__(**kwargs)
        self.encoder = build_transformer_layer_sequence(encoder)
        self.only_encoder = only_encoder

        self.decoder = build_transformer_layer_sequence(decoder)
        self.embed_dims = embed_dims
        self.num_feature_levels = num_feature_levels

        self.num_cams = num_cams
        self.fp16_enabled = False
        self.Z = Z
        self.D = D
        self.rotate_prev_bev = rotate_prev_bev
        self.use_can_bus = use_can_bus
        self.can_bus_norm = can_bus_norm
        self.use_shift = use_shift
        self.two_stage_num_proposals = two_stage_num_proposals
        self.init_layers()
        self.use_cams_embeds = use_cams_embeds
        self.count = 0

    def init_layers(self):
        """Initialize layers of the Detr3DTransformer."""
        self.level_embeds = nn.Parameter(torch.Tensor(self.num_feature_levels, self.embed_dims))
        self.cams_embeds = nn.Parameter(torch.Tensor(self.num_cams, self.embed_dims))
        self.reference_points = nn.Linear(self.embed_dims, 3)
        self.can_bus_mlp = nn.Sequential(
            nn.Linear(18, self.embed_dims // 2),
            nn.ReLU(inplace=True),
            nn.Linear(self.embed_dims // 2, self.embed_dims),
            nn.ReLU(inplace=True),
            #nn.LayerNorm(self.embed_dims)
        )

        if self.can_bus_norm:
            self.can_bus_mlp.add_module('norm', nn.LayerNorm(self.embed_dims))

    def init_weights(self):
        """Initialize the transformer weights."""
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for m in self.modules():
            if isinstance(m, CustomMultiScaleDeformableAttention)\
                    or isinstance(m, Detr3DCrossAtten)\
                    or isinstance(m, CustomMultiScaleDeformableAttentionV2) \
                    or isinstance(m, CustomMultiScaleDeformableAttentionV4) \
                    or isinstance(m, MultiScaleDeformableAttention3D):
                try:
                    m.init_weight()
                except AttributeError:
                    m.init_weights()
        normal_(self.level_embeds)
        normal_(self.cams_embeds)
        xavier_init(self.reference_points, distribution='uniform', bias=0.)
        xavier_init(self.can_bus_mlp, distribution='uniform', bias=0.)

    @staticmethod
    def get_reference_points(H, W, Z=8, D=4, dim='3d', bs=1, device='cuda', dtype=torch.float):
        """Get the reference points used in decoder.
        Args:
            H, W spatial shape of bev
            device (obj:`device`): The device where
                reference_points should be.
        Returns:
            Tensor: reference points used in decoder, has \
                shape (bs, num_keys, num_levels, 2).
        """

        if dim == '3d':

            zs = torch.linspace(0.5, Z - 0.5, D, dtype=dtype, device=device).view(-1, 1, 1).expand(D, H, W) / Z
            xs = torch.linspace(0.5, W - 0.5, W, dtype=dtype, device=device).view(1, 1, W).expand(D, H, W) / W
            ys = torch.linspace(0.5, H - 0.5, H, dtype=dtype, device=device).view(1, H, 1).expand(D, H, W) / H
            ref_3d = torch.stack((xs, ys, zs), -1)
            ref_3d = ref_3d.permute(0, 3, 1, 2).flatten(2).permute(0, 2, 1)

            ref_3d = ref_3d[None].repeat(bs, 1, 1, 1)
            return ref_3d
        elif dim == '2d':
            ref_y, ref_x = torch.meshgrid(torch.linspace(0.5, H - 0.5, H, dtype=dtype, device=device),
                                          torch.linspace(0.5, W - 0.5, W, dtype=dtype, device=device))
            ref_y = ref_y.reshape(-1)[None] / H  # ?
            ref_x = ref_x.reshape(-1)[None] / W  # ?
            ref_2d = torch.stack((ref_x, ref_y), -1)
            ref_2d = ref_2d.repeat(bs, 1, 1).unsqueeze(2)
            return ref_2d

    # @auto_fp16(apply_to=('mlvl_feats', 'bev_embed', 'query_embed'))
    #@run_time('BEVTransformer')
    def forward(
            self,
            mlvl_feats,
            bev_embed,
            query_embed,
            bev_h,
            bev_w,
            grid_length=0.512,
            bev_pos=None,
            reg_branches=None,
            cls_branches=None,
            prev_bev=None,
            return_bev=False,
            gt_bboxes_3d=None,  #used to debug
            **kwargs):
        """Forward function for `Detr3DTransformer`.
        Args:
            mlvl_feats (list(Tensor)): Input queries from
                different level. Each element has shape
                [bs, embed_dims, h, w].
            query_embed (Tensor): The query embedding for decoder,
                with shape [num_query, c].
            mlvl_pos_embeds (list(Tensor)): The positional encoding
                of feats from different level, has the shape
                 [bs, embed_dims, h, w].
            reg_branches (obj:`nn.ModuleList`): Regression heads for
                feature maps from each decoder layer. Only would
                be passed when
                `with_box_refine` is True. Default to None.
        Returns:
            tuple[Tensor]: results of decoder containing the following tensor.
                - inter_states: Outputs from decoder. If
                    return_intermediate_dec is True output has shape \
                      (num_dec_layers, bs, num_query, embed_dims), else has \
                      shape (1, bs, num_query, embed_dims).
                - init_reference_out: The initial value of reference \
                    points, has shape (bs, num_queries, 4).
                - inter_references_out: The internal value of reference \
                    points in decoder, has shape \
                    (num_dec_layers, bs,num_query, embed_dims)
                - enc_outputs_class: The classification score of \
                    proposals generated from \
                    encoder's feature maps, has shape \
                    (batch, h*w, num_classes). \
                    Only would be returned when `as_two_stage` is True, \
                    otherwise None.
                - enc_outputs_coord_unact: The regression results \
                    generated from encoder's feature maps., has shape \
                    (batch, h*w, 4). Only would \
                    be returned when `as_two_stage` is True, \
                    otherwise None.
        """

        bs = mlvl_feats[0].size(0)

        bev_embed = bev_embed.unsqueeze(1).repeat(1, bs, 1)
        bev_pos = bev_pos.flatten(2).permute(2, 0, 1)
        ref_3d = self.get_reference_points(bev_h,
                                           bev_w,
                                           self.Z,
                                           self.D,
                                           bs=bs,
                                           dim='3d',
                                           device=bev_embed.device,
                                           dtype=bev_embed.dtype)
        ref_2d = self.get_reference_points(bev_h,
                                           bev_w,
                                           dim='2d',
                                           bs=bs,
                                           device=bev_embed.device,
                                           dtype=bev_embed.dtype)

        # velocity = kwargs['img_metas'][0]['can_bus'][-3]
        # delta_time = 0.5 # the gap between two continuse samples is 0.5s
        delta_x = kwargs['img_metas'][0]['can_bus'][0]
        delta_y = kwargs['img_metas'][0]['can_bus'][1]

        ego_angle = kwargs['img_metas'][0]['can_bus'][-2] / np.pi * 180
        rotation_angle = kwargs['img_metas'][0]['can_bus'][-1]
        # assert bev_h == bev_w and bev_h == 200
        # grid_length = 0.512  # one grid in bev represents 0.512 meters
        if not isinstance(grid_length, tuple):
            grid_length_x = grid_length_y = grid_length
        else:
            grid_length_y = grid_length[0]
            grid_length_x = grid_length[1]
        translation_length = np.sqrt(delta_x**2 + delta_y**2)

        translation_angle = np.arctan2(delta_y, delta_x) / np.pi * 180
        if translation_angle < 0: translation_angle += 360

        bev_angle = ego_angle - translation_angle

        if self.use_shift == 'waymo_shift':
            shift_y = translation_length * np.sin(bev_angle / 180 * np.pi) / grid_length_y / bev_h
            shift_x = translation_length * np.cos(bev_angle / 180 * np.pi) / grid_length_x / bev_w
        elif self.use_shift:
            shift_y = translation_length * np.cos(bev_angle / 180 * np.pi) / grid_length_y / bev_h
            shift_x = translation_length * np.sin(bev_angle / 180 * np.pi) / grid_length_x / bev_w
            shift_y = shift_y  # * self.use_shift
            shift_x = shift_x  # * self.use_shift
        else:
            shift_y = 0
            shift_x = 0
        shift = bev_embed.new_tensor([shift_x, shift_y])

        if prev_bev is not None:
            if prev_bev.shape[1] == bev_h * bev_w:
                prev_bev = prev_bev.permute(1, 0, 2)

            if self.rotate_prev_bev == 'waymo_rotate':
                num_prev_bev = prev_bev.size(1)
                prev_bev = prev_bev.reshape(bev_h, bev_w, -1).permute(2, 0, 1)
                prev_bev = rotate(prev_bev, rotation_angle, center=[70, 150])
                prev_bev = prev_bev.permute(1, 2, 0).reshape(bev_h * bev_w, num_prev_bev, -1)

            elif self.rotate_prev_bev:

                # num_prev_bev = prev_bev.size(1)
                # re_order = [bev_h - i for i in range(1, bev_h + 1)]
                # prev_bev = prev_bev.reshape(bev_h, bev_w, -1)[re_order].permute(2, 0, 1)
                # # By considering the visualization results and basic physical knowledge,
                # I am sure this negative sign is correct
                # prev_bev = rotate(prev_bev, -rotation_angle)
                # prev_bev = prev_bev.permute(1, 2, 0)[re_order].reshape(bev_h * bev_w, num_prev_bev, -1)
                #
                # The following codes are same to the above

                num_prev_bev = prev_bev.size(1)
                prev_bev = prev_bev.reshape(bev_h, bev_w, -1).permute(2, 0, 1)
                prev_bev = rotate(prev_bev, rotation_angle)
                prev_bev = prev_bev.permute(1, 2, 0).reshape(bev_h * bev_w, num_prev_bev, -1)

        can_bus = bev_embed.new_tensor(kwargs['img_metas'][0]['can_bus'])[None, None, :]
        can_bus = self.can_bus_mlp(can_bus)
        bev_embed = bev_embed + can_bus * self.use_can_bus

        feat_flatten = []
        spatial_shapes = []
        for lvl, feat in enumerate(mlvl_feats):
            bs, num_cam, c, h, w = feat.shape
            spatial_shape = (h, w)
            feat = feat.flatten(3).permute(1, 0, 3, 2)
            if self.use_cams_embeds:
                feat = feat + self.cams_embeds[:, None, None, :].to(feat.dtype)
            else:
                feat = feat  # + self.cams_embeds[:, None, None, :].to(feat.dtype).sum()*0

            feat = feat + self.level_embeds[None, None, lvl:lvl + 1, :].to(feat.dtype)
            spatial_shapes.append(spatial_shape)
            feat_flatten.append(feat)

        feat_flatten = torch.cat(feat_flatten, 2)
        spatial_shapes = torch.as_tensor(spatial_shapes, dtype=torch.long, device=bev_pos.device)
        level_start_index = torch.cat((spatial_shapes.new_zeros((1, )), spatial_shapes.prod(1).cumsum(0)[:-1]))

        feat_flatten = feat_flatten.permute(0, 2, 1, 3)  # (num_cam, H*W, bs, embed_dims)

        bev_embed = self.encoder(bev_embed,
                                 feat_flatten,
                                 feat_flatten,
                                 ref_2d=ref_2d,
                                 ref_3d=ref_3d,
                                 bev_h=bev_h,
                                 bev_w=bev_w,
                                 bev_pos=bev_pos,
                                 spatial_shapes=spatial_shapes,
                                 level_start_index=level_start_index,
                                 prev_bev=prev_bev,
                                 shift=shift,
                                 gt_bboxes_3d=gt_bboxes_3d,
                                 **kwargs)

        if self.only_encoder:
            return bev_embed

        # if kwargs['img_metas'][0]['sample_idx'] == 'b6c420c3a5bd4a219b1cb82ee5ea0aa7':

        # if rotation_angle > 1:
        #     save_tensor(bev_embed.reshape(bev_h, bev_w, -1).permute(2, 0, 1), '{i}_main.png'.format(i=self.count))
        #     if prev_bev is not None:
        # #         save_tensor(prev_bev.reshape(bev_h, bev_w, -1).permute(2, 0, 1), '{i}_prev.png'.format(i=self.count))
        # if prev_bev is not None:
        #     prev_bev = prev_bev.reshape(bev_h, bev_w, -1).permute(2, 0, 1)
        #     # prev_bev = rotate(prev_bev, rotation_angle)
        #     save_tensor(prev_bev, '{i}_prev.png'.format(i=self.count))
        #     #

        # save_tensor(bev_embed.reshape(bev_h, bev_w, -1).permute(2, 0, 1), '{i}_main.png'.format(i=self.count))
        # self.count += 1

        query_pos, query = torch.split(query_embed, self.embed_dims, dim=1)
        query_pos = query_pos.unsqueeze(0).expand(bs, -1, -1)
        query = query.unsqueeze(0).expand(bs, -1, -1)
        reference_points = self.reference_points(query_pos)
        reference_points = reference_points.sigmoid()
        init_reference_out = reference_points

        # decoder
        query = query.permute(1, 0, 2)
        query_pos = query_pos.permute(1, 0, 2)
        bev_embed = bev_embed.permute(1, 0, 2)

        inter_states, inter_references = self.decoder(query=query,
                                                      key=None,
                                                      value=bev_embed,
                                                      query_pos=query_pos,
                                                      reference_points=reference_points,
                                                      reg_branches=reg_branches,
                                                      spatial_shapes=torch.tensor([[bev_h, bev_w]],
                                                                                  device=query.device),
                                                      level_start_index=torch.tensor([0], device=query.device),
                                                      key_padding_mask=self.encoder.key_padding_mask,
                                                      **kwargs)

        inter_references_out = inter_references

        if return_bev:
            return bev_embed, (inter_states, init_reference_out, inter_references_out)
        else:
            return inter_states, init_reference_out, inter_references_out
