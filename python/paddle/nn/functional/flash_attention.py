#   Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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

import paddle
import paddle.nn.functional as F
from paddle import _C_ops, in_dynamic_mode
from paddle.fluid.layer_helper import LayerHelper
from paddle.fluid.wrapped_decorator import signature_safe_contextmanager

g_enable_math = None
g_enable_flash = None
g_enable_mem_efficient = None


@signature_safe_contextmanager
def sdp_kernel(enable_math=False, enable_flash=True, enable_mem_efficient=True):
    r"""
    With the sdp_kernel context manager, different algorithm implementations can
    be selected for scaled_dot_product_attention.
    """
    global g_enable_math, g_enable_flash, g_enable_mem_efficient
    original_enable_math = g_enable_math
    original_enable_flash = g_enable_math
    original_enable_mem_efficient = g_enable_mem_efficient

    g_enable_math = enable_math
    g_enable_flash = enable_flash
    g_enable_mem_efficient = enable_mem_efficient
    try:
        yield
    finally:
        g_enable_math = original_enable_math
        g_enable_flash = original_enable_flash
        g_enable_mem_efficient = original_enable_mem_efficient


# special for XPU device
def get_triangle_upper_mask(x):
    mask = paddle.full_like(x, -1e4)
    mask.stop_gradient = True
    mask = paddle.triu(mask, diagonal=1)
    mask.stop_gradient = True
    return mask


def _math_attention(
    query,
    key,
    value,
    dropout_rate=0.0,
    causal=False,
    return_softmax=False,
    training=True,
):
    r"""
    This is a basic implementation of scaled dot product attention composed of
    combinations of fundamental components.
    """
    head_dim = query.shape[-1]
    query = paddle.transpose(query, [0, 2, 1, 3])
    key = paddle.transpose(key, [0, 2, 1, 3])
    value = paddle.transpose(value, [0, 2, 1, 3])
    product = paddle.matmul(
        x=query * (head_dim**-0.5), y=key, transpose_y=True
    )

    if not causal:
        weights = F.softmax(product)
    else:
        # special for XPU device
        place = paddle.get_device()
        if "xpu" in place:
            # softmax_mask_fuse_upper_triangle is not supported on XPU, use plain implementation
            mask = get_triangle_upper_mask(product)
            product = product + mask
            weights = F.softmax(product)
        else:
            weights = paddle.incubate.softmax_mask_fuse_upper_triangle(product)
    if dropout_rate > 0.0:
        weights = F.dropout(
            weights, dropout_rate, training=training, mode="upscale_in_train"
        )

    out = paddle.matmul(weights, value)
    out = paddle.transpose(out, [0, 2, 1, 3])
    return out, weights if return_softmax else None


def _select_sdp_cuda(head_dim):
    if head_dim <= 256:
        return "flash_attn"
    else:
        return "mem_efficient"


def _select_sdp(head_dim):
    r"""
    There are currently three different implementation options available for
    scaled dot product attention, and the chosen approach depends on whether it
    is determined by the sdp_kernel configuration or specified through input values.
    """
    place = paddle.get_device()
    # not use sdp_kernel
    if g_enable_flash is None:
        if "gpu" not in place:
            return "math"
        else:
            return _select_sdp_cuda(head_dim)

    if (
        g_enable_math is False
        and g_enable_flash is False
        and g_enable_mem_efficient is False
    ):
        raise AssertionError(
            "No available backend for scaled_dot_product_attention was found."
        )

    if g_enable_math is True:
        if g_enable_flash is False and g_enable_mem_efficient is False:
            return "math"
        if "gpu" not in place:
            return "math"
    if g_enable_flash is True and g_enable_mem_efficient is True:
        return _select_sdp_cuda(head_dim)
    if g_enable_flash is True:
        return "flash_attn"
    return "mem_efficient"


def flash_attention(
    query,
    key,
    value,
    dropout=0.0,
    causal=False,
    return_softmax=False,
    *,
    fixed_seed_offset=None,
    rng_name="",
    training=True,
    name=None,
):
    r"""
    The equation is:

    .. math::

        result=softmax(\frac{ Q * K^T }{\sqrt{d}}) * V

    where : ``Q``, ``K``, and ``V`` represent the three input parameters of the attention module.
    The dimensions of the three parameters are the same.
    ``d`` represents the size of the last dimension of the three parameters.

    Warning:
        This API is only support inputs with dtype float16 and bfloat16.

    Args:
        query(Tensor): The query tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        key(Tensor): The key tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        value(Tensor): The value tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        dropout(float): The dropout ratio.
        causal(bool): Whether enable causal mode.
        return_softmax(bool): Whether to return softmax.
        fixed_seed_offset(Tensor, optional): With fixed seed, offset for dropout mask.
        training(bool): Whether it is in the training phase.
        rng_name(str): The name to select Generator.
        name(str, optional): The default value is None. Normally there is no need for user
                        to set this property. For more information, please refer to
                        :ref:`api_guide_Name`.

    Returns:
        out(Tensor): The attention tensor.
                    4-D tensor with shape: [batch_size, seq_len, num_heads, head_dim].
                    The dtype can be float16 or bfloat16.
        softmax(Tensor): The softmax tensor. None if return_softmax is False.

    Examples:
        .. code-block:: python

            >>> import paddle

            >>> paddle.seed(2023)
            >>> q = paddle.rand((1, 128, 2, 16))

            >>> output = paddle.nn.functional.flash_attention.flash_attention(q, q, q, 0.9, False, False)
            >>> print(output)
            (Tensor(shape=[1, 128, 2, 16], dtype=float32, place=Place(cpu), stop_gradient=True,
            [[[[0.34992966, 0.34456208, 0.45826620, ..., 0.39883569,
                0.42132431, 0.39157745],
               [0.76687670, 0.65837246, 0.69117945, ..., 0.82817286,
                0.76690865, 0.71485823]],
              ...,
              [[0.71662450, 0.57275224, 0.57053083, ..., 0.48108247,
                0.53336465, 0.54540104],
               [0.59137970, 0.51350880, 0.50449550, ..., 0.38860250,
                0.40526697, 0.60541755]]]]), None)

    """
    head_dim = query.shape[3]
    sdp_func_name = _select_sdp(head_dim)

    if sdp_func_name == "flash_attn":
        if in_dynamic_mode():
            (
                result_attention,
                result_softmax,
            ) = _C_ops.flash_attn(
                query,
                key,
                value,
                fixed_seed_offset,
                None,
                dropout,
                causal,
                return_softmax,
                not training,
                rng_name,
            )
            return result_attention, result_softmax if return_softmax else None

        helper = LayerHelper('flash_attn', **locals())
        dtype = helper.input_dtype(input_param_name='q')
        out = helper.create_variable_for_type_inference(dtype)
        softmax = helper.create_variable_for_type_inference(dtype)
        softmax_lse = helper.create_variable_for_type_inference(paddle.float32)
        seed_offset = helper.create_variable_for_type_inference(paddle.int64)
        inputs = {
            'q': query,
            'k': key,
            'v': value,
            'fixed_seed_offset': fixed_seed_offset,
        }
        outputs = {
            'out': out,
            'softmax': softmax,
            'softmax_lse': softmax_lse,
            'seed_offset': seed_offset,
        }
        helper.append_op(
            type='flash_attn',
            inputs=inputs,
            outputs=outputs,
            attrs={
                'dropout': dropout,
                'causal': causal,
                'return_softmax': return_softmax,
                'is_test': not training,
                'rng_name': rng_name,
            },
        )
        return out, softmax if return_softmax else None
    else:
        if sdp_func_name == "mem_efficient":
            from paddle.incubate.nn.memory_efficient_attention import (
                memory_efficient_attention,
            )

            output = memory_efficient_attention(
                query,
                key,
                value,
                attn_bias=None,
                p=dropout,
                scale=None,
                training=training,
            )
            return output, None
        else:
            return _math_attention(
                query,
                key,
                value,
                dropout_rate=dropout,
                causal=causal,
                return_softmax=return_softmax,
                training=training,
            )


def flash_attn_unpadded(
    query,
    key,
    value,
    cu_seqlens_q,
    cu_seqlens_k,
    max_seqlen_q,
    max_seqlen_k,
    scale,
    dropout=0.0,
    causal=False,
    return_softmax=False,
    fixed_seed_offset=None,
    rng_name="",
    training=True,
    name=None,
):
    r"""
    The equation is:

    .. math::

        result=softmax(\frac{ Q * K^T }{\sqrt{d}}) * V

    where : ``Q``, ``K``, and ``V`` represent the three input parameters of the attention module.
    The dimensions of the three parameters are the same.
    ``d`` represents the size of the last dimension of the three parameters.

    Warning:
        This API is only support inputs with dtype float16 and bfloat16.

    Args:
        query(Tensor): The query tensor in the Attention module.
                        3-D tensor with shape:
                        [total_seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        key(Tensor): The key tensor in the Attention module.
                        3-D tensor with shape:
                        [total_seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        value(Tensor): The value tensor in the Attention module.
                        3-D tensor with shape:
                        [total_seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        cu_seqlens_q(Tensor): The cumulative sequence lengths of the sequences in the batch,
                        used to index query.
        cu_seqlens_k(Tensor): The cumulative sequence lengths of the sequences in the batch,
                        used to index key and value.
        max_seqlen_q(int): Maximum sequence length of query in the batch.
        max_seqlen_k(int): Maximum sequence length of key/value in the batch.
        scale(float): The scaling of QK^T before applying softmax.
        dropout(float): The dropout ratio.
        causal(bool): Whether enable causal mode.
        return_softmax(bool): Whether to return softmax.
        fixed_seed_offset(Tensor, optional): With fixed seed, offset for dropout mask.
        rng_name(str): The name to select Generator.
        training(bool): Whether it is in the training phase.
        name(str, optional): The default value is None. Normally there is no need for user
                        to set this property. For more information, please refer to
                        :ref:`api_guide_Name`.

    Returns:
        out(Tensor): The attention tensor.
                    4-D tensor with shape: [batch_size, seq_len, num_heads, head_dim].
                    The dtype can be float16 or bfloat16.
        softmax(Tensor): The softmax tensor. None if return_softmax is False.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> paddle.seed(2023)
            >>> q = paddle.rand((2, 128, 8, 16), dtype='float16')
            >>> cu = paddle.arange(0, 384, 128, dtype='int32')
            >>> qq = paddle.reshape(q, [256, 8, 16])
            >>> output = paddle.nn.functional.flash_attention.flash_attn_unpadded(qq, qq, qq, cu, cu, 128, 128, 0.25, 0.0, False, False)

    """
    if in_dynamic_mode():
        (
            result_attention,
            result_softmax,
        ) = _C_ops.flash_attn_unpadded(
            query,
            key,
            value,
            cu_seqlens_q,
            cu_seqlens_k,
            fixed_seed_offset,
            None,
            max_seqlen_q,
            max_seqlen_k,
            scale,
            dropout,
            causal,
            return_softmax,
            not training,
            rng_name,
        )
        return result_attention, result_softmax if return_softmax else None

    helper = LayerHelper('flash_attn_unpadded', **locals())
    dtype = helper.input_dtype(input_param_name='q')
    out = helper.create_variable_for_type_inference(dtype)
    softmax = helper.create_variable_for_type_inference(dtype)
    softmax_lse = helper.create_variable_for_type_inference(paddle.float32)
    seed_offset = helper.create_variable_for_type_inference(paddle.int64)
    inputs = {
        'q': query,
        'k': key,
        'v': value,
        'cu_seqlens_q': cu_seqlens_q,
        'cu_seqlens_k': cu_seqlens_k,
        'fixed_seed_offset': fixed_seed_offset,
    }
    outputs = {
        'out': out,
        'softmax': softmax,
        'softmax_lse': softmax_lse,
        'seed_offset': seed_offset,
    }
    helper.append_op(
        type='flash_attn_unpadded',
        inputs=inputs,
        outputs=outputs,
        attrs={
            'max_seqlen_q': max_seqlen_q,
            'max_seqlen_k': max_seqlen_k,
            'scale': scale,
            'dropout': dropout,
            'causal': causal,
            'return_softmax': return_softmax,
            'is_test': not training,
            'rng_name': rng_name,
        },
    )
    return out, softmax if return_softmax else None


def scaled_dot_product_attention(
    query,
    key,
    value,
    attn_mask=None,
    dropout_p=0.0,
    is_causal=False,
    training=True,
    name=None,
):
    r"""
    The equation is:

    .. math::

        result=softmax(\frac{ Q * K^T }{\sqrt{d}}) * V

    where : ``Q``, ``K``, and ``V`` represent the three input parameters of the attention module.
    The dimensions of the three parameters are the same.
    ``d`` represents the size of the last dimension of the three parameters.

    Warning:
        This API only supports inputs with dtype float16 and bfloat16.

    Args:
        query(Tensor): The query tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        key(Tensor): The key tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        value(Tensor): The value tensor in the Attention module.
                        4-D tensor with shape:
                        [batch_size, seq_len, num_heads, head_dim].
                        The dtype can be float61 or bfloat16.
        attn_mask(Tensor,optional): A float mask of the same type as query,
                        key, value that is added to the attention score.
        dropout_p(float): The dropout ratio.
        is_causal(bool): Whether enable causal mode.
        training(bool): Whether it is in the training phase.
        name(str, optional): The default value is None. Normally there is no need for user
                        to set this property. For more information, please refer to
                        :ref:`api_guide_Name`.

    Returns:
        out(Tensor): The attention tensor.
                    4-D tensor with shape: [batch_size, seq_len, num_heads, head_dim].
                    The dtype can be float16 or bfloat16.

    Examples:
        .. code-block:: python

            >>> # doctest: +SKIP('bfloat need V100 compile')
            >>> import paddle
            >>> q = paddle.rand((1, 128, 2, 16), dtype=paddle.bfloat16)
            >>> output = paddle.nn.functional.scaled_dot_product_attention(q, q, q, None, 0.9, False)
            >>> print(output)
            >>> # doctest: -SKIP
    """
    if attn_mask is None:
        out, _ = flash_attention(query, key, value, dropout_p, is_causal)
    else:
        fixed_seed_offset = (None,)
        return_softmax = False
        rng_name = ""
        out, _ = _C_ops.flash_attn(
            query,
            key,
            value,
            fixed_seed_offset,
            attn_mask,
            dropout_p,
            is_causal,
            return_softmax,
            not training,
            rng_name,
        )
    return out
