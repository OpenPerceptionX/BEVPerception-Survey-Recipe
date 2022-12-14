# ==============================================================================
# Binaries and/or source for the following packages or projects are presented under one or more of the following open
# source licenses:
# multi_scale_deformable_attn_3d.py       OpenPerceptionX        Apache License, Version 2.0
#
# Contact simachonghao@pjlab.org.cn if you have any issue
#
# See:
# https://github.com/zhiqi-li/BEVFormer/blob/master/projects/mmdet3d_plugin/bevformer/modules/multi_scale_deformable_attn_function.py
#
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


import math
import warnings
import torch
import torch.nn as nn
from mmcv.cnn import xavier_init, constant_init
from mmcv.cnn.bricks.registry import ATTENTION
from mmcv.runner.base_module import BaseModule
from mmcv.utils import ext_loader

ext_module = ext_loader.load_ext('_ext', ['ms_deform_attn_backward', 'ms_deform_attn_forward'])
from mmcv.ops.multi_scale_deform_attn import multi_scale_deformable_attn_pytorch
from ..attns.multi_scale_deformable_attn_function import MultiScaleDeformableAttnFunction_fp32


@ATTENTION.register_module()
class MultiScaleDeformableAttention3D(BaseModule):
    """An attention module used in Deformable-Detr.
    `Deformable DETR: Deformable Transformers for End-to-End Object Detection.
    <https://arxiv.org/pdf/2010.04159.pdf>`_.
    Args:
        embed_dims (int): The embedding dimension of Attention.
            Default: 256.
        num_heads (int): Parallel attention heads. Default: 64.
        num_levels (int): The number of feature map used in
            Attention. Default: 4.
        num_points (int): The number of sampling points for
            each query in each head. Default: 4.
        im2col_step (int): The step used in image_to_column.
            Default: 64.
        dropout (float): A Dropout layer on `inp_identity`.
            Default: 0.1.
        batch_first (bool): Key, Query and Value are shape of
            (batch, n, embed_dim)
            or (n, batch, embed_dim). Default to False.
        norm_cfg (dict): Config dict for normalization layer.
            Default: None.
        init_cfg (obj:`mmcv.ConfigDict`): The Config for initialization.
            Default: None.
    """

    def __init__(self,
                 embed_dims=256,
                 num_heads=8,
                 num_levels=4,
                 num_points=8,
                 im2col_step=64,
                 dropout=0.1,
                 batch_first=False,
                 norm_cfg=None,
                 init_cfg=None):
        super().__init__(init_cfg)
        if embed_dims % num_heads != 0:
            raise ValueError(f'embed_dims must be divisible by num_heads, '
                             f'but got {embed_dims} and {num_heads}')
        dim_per_head = embed_dims // num_heads
        self.norm_cfg = norm_cfg
        self.batch_first = batch_first
        self.output_proj = None
        self.fp16_enabled = False

        # you'd better set dim_per_head to a power of 2
        # which is more efficient in the CUDA implementation
        def _is_power_of_2(n):
            if (not isinstance(n, int)) or (n < 0):
                raise ValueError('invalid input for _is_power_of_2: {} (type: {})'.format(n, type(n)))
            return (n & (n - 1) == 0) and n != 0

        if not _is_power_of_2(dim_per_head):
            warnings.warn("You'd better set embed_dims in "
                          'MultiScaleDeformAttention to make '
                          'the dimension of each attention head a power of 2 '
                          'which is more efficient in our CUDA implementation.')

        self.im2col_step = im2col_step
        self.embed_dims = embed_dims
        self.num_levels = num_levels
        self.num_heads = num_heads
        self.num_points = num_points
        self.sampling_offsets = nn.Linear(embed_dims, num_heads * num_levels * num_points * 2)
        self.attention_weights = nn.Linear(embed_dims, num_heads * num_levels * num_points)
        self.value_proj = nn.Linear(embed_dims, embed_dims)

        self.init_weights()

    def init_weights(self):
        """Default initialization for Parameters of Module."""
        constant_init(self.sampling_offsets, 0.)
        thetas = torch.arange(self.num_heads, dtype=torch.float32) * (2.0 * math.pi / self.num_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (grid_init / grid_init.abs().max(-1, keepdim=True)[0]).view(self.num_heads, 1, 1, 2).repeat(
            1, self.num_levels, self.num_points, 1)
        for i in range(self.num_points):
            grid_init[:, :, i, :] *= i + 1

        self.sampling_offsets.bias.data = grid_init.view(-1)
        constant_init(self.attention_weights, val=0., bias=0.)
        xavier_init(self.value_proj, distribution='uniform', bias=0.)
        xavier_init(self.output_proj, distribution='uniform', bias=0.)
        self._is_init = True

    # @auto_fp16(apply_to=('query', 'value', 'identity', 'query_pos', 'key_padding_mask', 'reference_points'))
    def forward(self,
                query,
                key=None,
                value=None,
                identity=None,
                query_pos=None,
                key_padding_mask=None,
                reference_points=None,
                spatial_shapes=None,
                level_start_index=None,
                **kwargs):
        """Forward Function of MultiScaleDeformAttention.
        Args:
            query (Tensor): Query of Transformer with shape
                (num_query, bs, embed_dims).
            key (Tensor): The key tensor with shape
                `(num_key, bs, embed_dims)`.
            value (Tensor): The value tensor with shape
                `(num_key, bs, embed_dims)`.
            identity (Tensor): The tensor used for addition, with the
                same shape as `query`. Default None. If None,
                `query` will be used.
            query_pos (Tensor): The positional encoding for `query`.
                Default: None.
            key_pos (Tensor): The positional encoding for `key`. Default
                None.
            reference_points (Tensor):  The normalized reference
                points with shape (bs, num_query, num_levels, 2),
                all elements is range in [0, 1], top-left (0,0),
                bottom-right (1, 1), including padding area.
                or (N, Length_{query}, num_levels, 4), add
                additional two dimensions is (w, h) to
                form reference boxes.
            key_padding_mask (Tensor): ByteTensor for `query`, with
                shape [bs, num_key].
            spatial_shapes (Tensor): Spatial shape of features in
                different levels. With shape (num_levels, 2),
                last dimension represents (h, w).
            level_start_index (Tensor): The start index of each level.
                A tensor has shape ``(num_levels, )`` and can be represented
                as [0, h_0*w_0, h_0*w_0+h_1*w_1, ...].
        Returns:
             Tensor: forwarded results with shape [num_query, bs, embed_dims].
        """

        if value is None:
            value = query
        if identity is None:
            identity = query
        if query_pos is not None:
            query = query + query_pos

        if not self.batch_first:
            # change to (bs, num_query ,embed_dims)
            query = query.permute(1, 0, 2)
            value = value.permute(1, 0, 2)

        bs, num_query, _ = query.shape
        bs, num_value, _ = value.shape
        assert (spatial_shapes[:, 0] * spatial_shapes[:, 1]).sum() == num_value

        value = self.value_proj(value)
        if key_padding_mask is not None:
            value = value.masked_fill(key_padding_mask[..., None], 0.0)
        value = value.view(bs, num_value, self.num_heads, -1)
        sampling_offsets = self.sampling_offsets(query).view(bs, num_query, self.num_heads, self.num_levels,
                                                             self.num_points, 2)
        attention_weights = self.attention_weights(query).view(bs, num_query, self.num_heads,
                                                               self.num_levels * self.num_points)

        attention_weights = attention_weights.softmax(-1)

        attention_weights = attention_weights.view(bs, num_query, self.num_heads, self.num_levels, self.num_points)
        if reference_points.shape[-1] == 2:
            # N_levels 2
            offset_normalizer = torch.stack([spatial_shapes[..., 1], spatial_shapes[..., 0]], -1)

            # BS, N_query, N_Head, N_level, N_points, (x,y)

            # BS, N_query, _, N_level, _ (num_height), (x,y)
            reference_points = reference_points[:, :, None, :, None, :]
            s1, s2, s3, s4, s5, s6 = reference_points.shape

            reference_points = reference_points.view(s1, s2, s3, s5, 1, s4, s6)
            sampling_offsets = sampling_offsets / offset_normalizer[None, None, None, :, None, :]
            c1, c2, c3, c4, c5, c6 = sampling_offsets.shape
            assert c4 == self.num_levels
            sampling_offsets = sampling_offsets.view(c1, c2, c3, c4, c5 // s4, s4, c6)
            sampling_locations = reference_points + sampling_offsets
            t1, t2, t3, t4, t5, t6, t7 = sampling_locations.shape
            sampling_locations = sampling_locations.view(t1, t2, t3, t4, t5 * t6, t7)

        elif reference_points.shape[-1] == 4:
            assert False
            sampling_locations = reference_points[:, :, None, :, None, :2] \
                                 + sampling_offsets / self.num_points \
                                 * reference_points[:, :, None, :, None, 2:] \
                                 * 0.5
        else:
            raise ValueError(f'Last dim of reference_points must be'
                             f' 2 or 4, but get {reference_points.shape[-1]} instead.')
        # print('sampling_locations', sampling_locations.shape)  # bs, num_query, num_heads, scales, num_points, 2
        # print('attention_weights', attention_weights.shape)  # bs, num_query, num_heads, scales, num_points
        #
        if torch.cuda.is_available() and value.is_cuda:
            if value.dtype == torch.float16:
                MultiScaleDeformableAttnFunction = MultiScaleDeformableAttnFunction_fp32
            else:
                MultiScaleDeformableAttnFunction = MultiScaleDeformableAttnFunction_fp32
            output = MultiScaleDeformableAttnFunction.apply(value, spatial_shapes, level_start_index,
                                                            sampling_locations, attention_weights, self.im2col_step)
        else:
            output = multi_scale_deformable_attn_pytorch(value, spatial_shapes, sampling_locations, attention_weights)
        if not self.batch_first:
            # (num_query, bs ,embed_dims)
            output = output.permute(1, 0, 2)

        return output
