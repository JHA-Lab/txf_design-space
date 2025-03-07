# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""PyTorch BERT model. """

#Modified by: Bhishma Dedhia

import math
import os
import warnings
from dataclasses import dataclass
from typing import Optional, Tuple
import copy 

import torch
import torch.utils.checkpoint
from torch import nn
from torch.nn import CrossEntropyLoss, MSELoss


from ...activations import ACT2FN
from ...file_utils import (
    ModelOutput,
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from ...modeling_outputs import (
    BaseModelOutputWithPastAndCrossAttentions,
    BaseModelOutputWithPoolingAndCrossAttentions,
    CausalLMOutputWithCrossAttentions,
    MaskedLMOutput,
    MultipleChoiceModelOutput,
    NextSentencePredictorOutput,
    QuestionAnsweringModelOutput,
    SequenceClassifierOutput,
    TokenClassifierOutput,
)
from ...modeling_utils import (
    PreTrainedModel,
    apply_chunking_to_forward,
    find_pruneable_heads_and_indices,
    prune_linear_layer,
)
from ...utils import logging
from .configuration_bert import BertConfig
from .dct import dct_2d
from .modeling_bert import BertPreTrainedModel, BertForPreTrainingOutput

from sklearn import random_projection
import numpy as np


logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "bert-base-uncased"
_CONFIG_FOR_DOC = "BertConfig"
_TOKENIZER_FOR_DOC = "BertTokenizer"

BERT_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "bert-base-uncased",
    "bert-large-uncased",
    "bert-base-cased",
    "bert-large-cased",
    "bert-base-multilingual-uncased",
    "bert-base-multilingual-cased",
    "bert-base-chinese",
    "bert-base-german-cased",
    "bert-large-uncased-whole-word-masking",
    "bert-large-cased-whole-word-masking",
    "bert-large-uncased-whole-word-masking-finetuned-squad",
    "bert-large-cased-whole-word-masking-finetuned-squad",
    "bert-base-cased-finetuned-mrpc",
    "bert-base-german-dbmdz-cased",
    "bert-base-german-dbmdz-uncased",
    "cl-tohoku/bert-base-japanese",
    "cl-tohoku/bert-base-japanese-whole-word-masking",
    "cl-tohoku/bert-base-japanese-char",
    "cl-tohoku/bert-base-japanese-char-whole-word-masking",
    "TurkuNLP/bert-base-finnish-cased-v1",
    "TurkuNLP/bert-base-finnish-uncased-v1",
    "wietsedv/bert-base-dutch-cased",
    # See all BERT models at https://huggingface.co/models?filter=bert
]



class BertEmbeddingsModular(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""

    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_dim_list[0], padding_idx=config.pad_token_id)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_dim_list[0])
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_dim_list[0])

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = nn.LayerNorm(config.hidden_dim_list[0], eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # position_ids (1, len position emb) is contiguous in memory and exported when serialized
        self.register_buffer("position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)))
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")

    def forward(
        self, input_ids=None, token_type_ids=None, position_ids=None, inputs_embeds=None, past_key_values_length=0
    ):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]

        if position_ids is None:
            position_ids = self.position_ids[:, past_key_values_length : seq_length + past_key_values_length]

        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=self.position_ids.device)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = inputs_embeds + token_type_embeddings
        if self.position_embedding_type == "absolute":
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings

#Adding BERT Self Attention
class BertSelfAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()
        if config.hidden_dim_list[layer_id] % config.attention_heads_list[layer_id] != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
                f"heads ({config.num_attention_heads})"
            )

        self.num_attention_heads = config.attention_heads_list[layer_id]
        self.hidden_size = config.hidden_dim_list[layer_id]
        self.attention_head_size = int(self.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(self.hidden_size, self.all_head_size)
        self.key = nn.Linear(self.hidden_size, self.all_head_size)
        self.value = nn.Linear(self.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(2 * config.max_position_embeddings - 1, self.attention_head_size)

        self.is_decoder = config.is_decoder
        self.sim = config.similarity_list[layer_id]
        self.W = torch.nn.Parameter(torch.FloatTensor(self.attention_head_size,self.attention_head_size).uniform_(-0.1, 0.1))

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):

        mixed_query_layer = self.query(hidden_states)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        is_cross_attention = encoder_hidden_states is not None

        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_layer = past_key_value[0]
            value_layer = past_key_value[1]
            attention_mask = encoder_attention_mask
        elif is_cross_attention:
            key_layer = self.transpose_for_scores(self.key(encoder_hidden_states))
            value_layer = self.transpose_for_scores(self.value(encoder_hidden_states))
            attention_mask = encoder_attention_mask
        elif past_key_value is not None:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))
            key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
            value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
        else:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)

        if self.is_decoder:
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_layer, value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        if self.sim=='sdp':
            attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))

        elif self.sim=='wma':
            attention_scores= torch.matmul(torch.matmul(query_layer,self.W), key_layer.transpose(-1, -2))


        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":

            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key

        
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)


        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        if self.is_decoder:
            outputs = outputs + (past_key_value,)


        return outputs


# Adapted from https://github.com/google-research/google-research/blob/master/f_net/fourier.py
def fftn(x):
    """
    Applies n-dimensional Fast Fourier Transform (FFT) to input array.
    Args:
        x: Input n-dimensional array.
    Returns:
        n-dimensional Fourier transform of input n-dimensional array.
    """
    out = x
    for axis in reversed(range(x.ndim)[1:]):  # We don't need to apply FFT to last axis
        out = torch.fft.fft(out, axis=axis)
    return out


class SeparableConv1D(nn.Module):
    """This class implements separable convolution, i.e. a depthwise and a pointwise layer"""

    def __init__(self, config, input_filters, output_filters, kernel_size, **kwargs):
        super().__init__()
        self.depthwise = nn.Conv1d(
            input_filters,
            input_filters,
            kernel_size=kernel_size,
            groups=input_filters,
            padding=kernel_size // 2,
            bias=False,
        )
        self.pointwise = nn.Conv1d(input_filters, output_filters, kernel_size=1, bias=False)
        self.bias = nn.Parameter(torch.zeros(output_filters, 1))

        self.depthwise.weight.data.normal_(mean=0.0, std=config.initializer_range)
        self.pointwise.weight.data.normal_(mean=0.0, std=config.initializer_range)

    def forward(self, hidden_states):
        x = self.depthwise(hidden_states)
        x = self.pointwise(x)
        x += self.bias
        return x


# Adding a heterogenous attention module
class BertHeteroAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()

        assert config.from_model_dict_hetero is True, 'Heterogeneous attention only with model_dict_hetero'

        # if config.hidden_dim_list[layer_id] % len(config.attention_heads_list[layer_id]) != 0 and not hasattr(config, "embedding_size"):
        #     raise ValueError(
        #         f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
        #         f"heads ({config.num_attention_heads})"
        #     )

        self.num_attention_heads = len(config.attention_heads_list[layer_id])
        self.hidden_size = config.hidden_dim_list[layer_id]

        # Fixing attention_head_size to specified values for grow-and-prune weight transfer
        # self.attention_head_size = int(self.hidden_size / self.num_attention_heads)
        # self.attention_head_size = int(self.hidden_size / 2 ** math.floor(math.log(self.num_attention_heads, 2)))
        attention_head_sizes = [int(attention.split('_')[2]) for attention in config.attention_heads_list[layer_id]]
        assert len(set(attention_head_sizes)) == 1, f'All attention heads should have the same size for layer ID: {layer_id}'
        self.attention_head_size = attention_head_sizes[0]

        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(self.hidden_size, self.all_head_size)
        self.key = nn.Linear(self.hidden_size, self.all_head_size)
        self.value = nn.Linear(self.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(2 * config.max_position_embeddings - 1, self.attention_head_size)

        self.is_decoder = config.is_decoder

        self.attention_types = [attention.split('_')[0] for attention in config.attention_heads_list[layer_id]]
        self.sim_types = [attention.split('_')[1] for attention in config.attention_heads_list[layer_id]]

        wma_count, conv_count = 0, 0
        for sim_type in self.sim_types:
            if sim_type == 'wma':
                setattr(self, f'W{wma_count}', torch.nn.Parameter(
                    torch.FloatTensor(self.attention_head_size, self.attention_head_size).uniform_(-0.1, 0.1)))
                wma_count += 1
            elif sim_type.isnumeric():
                setattr(self, f'key_conv_attn_layer{conv_count}', SeparableConv1D(
                    config, self.attention_head_size, self.attention_head_size, int(sim_type)))
                setattr(self, f'conv_kernel_layer{conv_count}', nn.Linear(
                    self.attention_head_size, int(sim_type)))
                setattr(self, f'conv_out_layer{conv_count}', nn.Linear(
                    self.attention_head_size, self.attention_head_size))
                setattr(self, f'unfold{conv_count}', nn.Unfold(
                    kernel_size=[int(sim_type), 1], padding=[int((int(sim_type) - 1) / 2), 0]))
                conv_count += 1


    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        
        mixed_query_layer = self.query(hidden_states)
        batch_size = hidden_states.size(0)
        max_seq_length = hidden_states.size(1)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        is_cross_attention = encoder_hidden_states is not None

        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_layer = past_key_value[0]
            value_layer = past_key_value[1]
            attention_mask = encoder_attention_mask
        elif is_cross_attention:
            key_layer = self.transpose_for_scores(self.key(encoder_hidden_states))
            value_layer = self.transpose_for_scores(self.value(encoder_hidden_states))
            attention_mask = encoder_attention_mask
        elif past_key_value is not None:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))
            key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
            value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
        else:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)

        if self.is_decoder:
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_layer, value_layer)

        attention_scores_size = query_layer.size()[:-1] + (query_layer.size()[-2],)
        # attention_scores = torch.zeros(*attention_scores_size).to(device=hidden_states.device)

        attention_scores_list = []
        wma_count = 0
        for attention_head in range(self.num_attention_heads):
            if self.attention_types[attention_head] == 'sa':
                if self.sim_types[attention_head] == 'sdp':
                    # Take the dot product between "query" and "key" to get the raw attention scores.
                    attention_scores_list.append(torch.matmul(query_layer[:, attention_head, :, :], 
                        key_layer[:, attention_head, :, :].transpose(-1, -2)))
                elif self.sim_types[attention_head] == 'wma':
                    # Take a weighted multiplicative addition between "query" and "key" vectors.
                    attention_scores_list.append(torch.matmul(torch.matmul(query_layer[:, attention_head, :, :], getattr(self, f'W{wma_count}')), 
                        key_layer[:, attention_head, :, :].transpose(-1, -2)))
                    wma_count += 1
            elif self.attention_types[attention_head] == 'l':
                # Attention operation not used in linear-transform based attention head.
                # Attention scores only used for relative encodings.
                attention_scores_list.append(torch.zeros(*[s for i, s in enumerate(attention_scores_size) if i != 1]).to(device=hidden_states.device))
            elif self.attention_types[attention_head] == 'c':
                # Attention operation not used in convolution based attention head.
                # Attention scores only used for relative encodings.
                attention_scores_list.append(torch.zeros(*[s for i, s in enumerate(attention_scores_size) if i != 1]).to(device=hidden_states.device))

        attention_scores = torch.stack(attention_scores_list, 1)

        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":

            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key
        
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)
        # print(f'context_layer.size(): {context_layer.size()}')

        conv_count = 0
        context_layer_list = []
        for attention_head in range(self.num_attention_heads):
            if self.attention_types[attention_head] == 'sa':
                context_layer_list.append(torch.zeros(*[batch_size, max_seq_length, self.attention_head_size]).to(device=hidden_states.device))
            elif self.attention_types[attention_head] == 'l':
                if self.sim_types[attention_head] == 'dft':
                    fft_output = fftn(value_layer[:, attention_head, :, :]).real
                    # Add fft to relative position embeddings
                    context_layer_list.append(context_layer[:, attention_head, :, :] + fft_output)

                elif self.sim_types[attention_head] == 'dct':
                    dct_output = dct_2d(value_layer[:, attention_head, :, :])
                    # Add dct to relative position embeddings
                    context_layer_list.append(context_layer[:, attention_head, :, :] + dct_output)
            elif self.attention_types[attention_head] == 'c':
                mixed_key_conv_attn_layer = getattr(self, f'key_conv_attn_layer{conv_count}')(
                    key_layer[:, attention_head, :, :].transpose(1, 2))
                mixed_key_conv_attn_layer = mixed_key_conv_attn_layer.transpose(1, 2)
                # print(f'mixed_key_conv_attn_layer.size(): {mixed_key_conv_attn_layer.size()}')

                conv_attn_layer = torch.multiply(mixed_key_conv_attn_layer, query_layer[:, attention_head, :, :])
                conv_kernel_layer = getattr(self, f'conv_kernel_layer{conv_count}')(conv_attn_layer)
                # print(f'conv_kernel_layer.size(): {conv_kernel_layer.size()}')
                conv_kernel_layer = torch.reshape(conv_kernel_layer, [-1, int(self.sim_types[attention_head]), 1])
                conv_kernel_layer = torch.softmax(conv_kernel_layer, dim=1)
                # print(f'conv_kernel_layer.size() after reshape: {conv_kernel_layer.size()}')

                conv_out_layer = getattr(self, f'conv_out_layer{conv_count}')(value_layer[:, attention_head, :, :])
                conv_out_layer = torch.reshape(conv_out_layer, [batch_size, -1, self.attention_head_size])
                conv_out_layer = conv_out_layer.transpose(1, 2).contiguous().unsqueeze(-1)
                conv_out_layer = getattr(self, f'unfold{conv_count}')(conv_out_layer)
                conv_out_layer = conv_out_layer.transpose(1, 2).reshape(
                    batch_size, -1, self.attention_head_size, int(self.sim_types[attention_head]))
                # print(f'conv_out_layer.size(): {conv_out_layer.size()}')
                conv_out_layer = torch.reshape(conv_out_layer, [-1, self.attention_head_size, int(self.sim_types[attention_head])])
                # print(f'conv_out_layer.size() after reshape: {conv_out_layer.size()}')
                conv_out_layer = torch.matmul(conv_out_layer, conv_kernel_layer)
                conv_out_layer = torch.reshape(conv_out_layer, [-1, self.attention_head_size])

                conv_out = torch.reshape(conv_out_layer, [batch_size, -1, self.attention_head_size])
                conv_count += 1
                
                context_layer_list.append(context_layer[:, attention_head, :, :] + conv_out)


        context_layer = torch.stack(context_layer_list, 1)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        if self.is_decoder:
            outputs = outputs + (past_key_value,)

        # print(f'outputs[0].size(): {outputs[0].size()}')

        return outputs


#For dft and dct
class BertLinearAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()
        if config.hidden_dim_list[layer_id] % config.attention_heads_list[layer_id] != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                f"The hidden size ({ config.hidden_dim_list[layer_id] }) is not a multiple of the number of attention "
                f"heads ({config.attention_heads_list[layer_id]})"
            )

        self.num_attention_heads = config.attention_heads_list[layer_id]
        self.hidden_size = config.hidden_dim_list[layer_id]
        self.attention_head_size = int(self.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(self.hidden_size, self.all_head_size)
        self.key = nn.Linear(self.hidden_size, self.all_head_size)
        self.value = nn.Linear(self.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(2 * config.max_position_embeddings - 1, self.attention_head_size)

        self.is_decoder = config.is_decoder
        self.sim = config.similarity_list[layer_id]
        #self.W = torch.nn.Parameter(torch.FloatTensor(self.attention_head_size,self.attention_head_size).uniform_(-0.1, 0.1))

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        
        mixed_query_layer = self.query(hidden_states)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        is_cross_attention = encoder_hidden_states is not None

        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_layer = past_key_value[0]
            value_layer = past_key_value[1]
            attention_mask = encoder_attention_mask
        elif is_cross_attention:
            key_layer = self.transpose_for_scores(self.key(encoder_hidden_states))
            value_layer = self.transpose_for_scores(self.value(encoder_hidden_states))
            attention_mask = encoder_attention_mask
        elif past_key_value is not None:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))
            key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
            value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
        else:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)

        if self.is_decoder:
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_layer, value_layer)

        
        attention_scores = torch.zeros(hidden_states.shape[0],self.num_attention_heads,hidden_states.shape[1],hidden_states.shape[1]).to(device=hidden_states.device)

        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                #debug --> print(attention_scores.device(),relative_position_scores.device)
                attention_scores = attention_scores + relative_position_scores

            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key

        
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        

        if self.sim == 'dft':
            fft_output = torch.fft.fft(torch.fft.fft(hidden_states, dim=-1), dim=-2).real
            #Add fft to relative position embeddings
            context_layer += fft_output

        elif self.sim == 'dct':
            dct_output = dct_2d(hidden_states)
            #Add fft to relative position embeddings
            context_layer += dct_output

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        if self.is_decoder:
            outputs = outputs + (past_key_value,)

        return outputs

class BertSelfOutputModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()

        if config.from_model_dict_hetero is True:
            num_attention_heads = len(config.attention_heads_list[layer_id])
            attention_head_size = int(config.attention_heads_list[layer_id][0].split('_')[2])
            all_head_size = num_attention_heads * attention_head_size
        else:
            all_head_size = config.hidden_dim_list[layer_id]

        self.dense = nn.Linear(all_head_size, config.hidden_dim_list[layer_id])
        self.LayerNorm = nn.LayerNorm(config.hidden_dim_list[layer_id], eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()

        if config.from_model_dict_hetero is True:
            self.self = BertHeteroAttentionModular(config, layer_id)
        else:
            if not(config.attention_type[layer_id] == 'sa' or config.attention_type[layer_id] == 'l'):
                raise ValueError(
                    f"Incorrect attention type specified"
                )

            if config.attention_type[layer_id] == 'sa':
                self.self = BertSelfAttentionModular(config, layer_id)
            elif config.attention_type[layer_id] == 'l':
                self.self = BertLinearAttentionModular(config, layer_id)

        self.output = BertSelfOutputModular(config, layer_id)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            past_key_value,
            output_attentions,
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


class BertIntermediateModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()

        self.dense = nn.Linear(config.hidden_dim_list[layer_id], config.ff_dim_list[layer_id][0])
        if isinstance(config.hidden_act, str):
            
            if config.hidden_act == "gelu":
                self.intermediate_act_fn = nn.GELU()

            else:
                self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

        modules = []

        modules.append(self.dense)
        modules.append(self.intermediate_act_fn)

        for i in range(len(config.ff_dim_list[layer_id])-1):

            modules.append(nn.Linear(config.ff_dim_list[layer_id][i], config.ff_dim_list[layer_id][i+1]))
            modules.append(self.intermediate_act_fn)

        self.sequential = nn.Sequential(*modules)

    def forward(self, hidden_states):
       
        return self.sequential(hidden_states)


class BertOutputModular(nn.Module):
    def __init__(self, config, layer_id, last_layer):
        super().__init__()
        self.dense = nn.Linear(config.ff_dim_list[layer_id][-1], config.hidden_dim_list[layer_id])
        self.LayerNorm = nn.LayerNorm(config.hidden_dim_list[layer_id], eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.last_layer = last_layer
        if last_layer:
                self.proj_required = False

        else:
            if  config.hidden_dim_list[layer_id]==config.hidden_dim_list[layer_id+1]:
                self.proj_required = False
            else:
                self.proj_required = True

        if self.proj_required:
            self.proj_head = nn.Linear(config.hidden_dim_list[layer_id],config.hidden_dim_list[layer_id+1])

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        if self.proj_required:
            hidden_states = self.proj_head(hidden_states)
        return hidden_states


class BertLayerModular(nn.Module):
    def __init__(self, config, layer_id, last_layer = False):
        super().__init__()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = BertAttentionModular(config,layer_id)
        self.is_decoder = config.is_decoder
        self.add_cross_attention = config.add_cross_attention
        if self.add_cross_attention:
            assert self.is_decoder, f"{self} should be used as a decoder model if cross attention is added"
            self.crossattention = BertAttentionModular(config,layer_id)
        self.intermediate = BertIntermediateModular(config,layer_id)
        self.output = BertOutputModular(config,layer_id,last_layer)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        # decoder uni-directional self-attention cached key/values tuple is at positions 1,2
        self_attn_past_key_value = past_key_value[:2] if past_key_value is not None else None
        self_attention_outputs = self.attention(
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions=output_attentions,
            past_key_value=self_attn_past_key_value,
        )
        attention_output = self_attention_outputs[0]

        # if decoder, the last output is tuple of self-attn cache
        if self.is_decoder:
            outputs = self_attention_outputs[1:-1]
            present_key_value = self_attention_outputs[-1]
        else:
            outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        cross_attn_present_key_value = None
        if self.is_decoder and encoder_hidden_states is not None:
            assert hasattr(
                self, "crossattention"
            ), f"If `encoder_hidden_states` are passed, {self} has to be instantiated with cross-attention layers by setting `config.add_cross_attention=True`"

            # cross_attn cached key/values tuple is at positions 3,4 of past_key_value tuple
            cross_attn_past_key_value = past_key_value[-2:] if past_key_value is not None else None
            cross_attention_outputs = self.crossattention(
                attention_output,
                attention_mask,
                head_mask,
                encoder_hidden_states,
                encoder_attention_mask,
                cross_attn_past_key_value,
                output_attentions,
            )
            attention_output = cross_attention_outputs[0]
            outputs = outputs + cross_attention_outputs[1:-1]  # add cross attentions if we output attention weights

            # add cross-attn cache to positions 3,4 of present_key_value tuple
            cross_attn_present_key_value = cross_attention_outputs[-1]
            present_key_value = present_key_value + cross_attn_present_key_value

        layer_output = apply_chunking_to_forward(
            self.feed_forward_chunk, self.chunk_size_feed_forward, self.seq_len_dim, attention_output
        )
        outputs = (layer_output,) + outputs

        # if decoder, return the attn key/values as the last output
        if self.is_decoder:
            outputs = outputs + (present_key_value,)

        return outputs

    def feed_forward_chunk(self, attention_output):
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output


class ConvBertSelfAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()
        if config.hidden_dim_list[layer_id] % config.attention_heads_list[layer_id] != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                f"The hidden size ({config.hidden_dim_list[layer_id]}) is not a multiple of the number of attention "
                f"heads ({ config.attention_heads_list[layer_id]})"
            )

        new_num_attention_heads = config.attention_heads_list[layer_id] // config.head_ratio
        if new_num_attention_heads < 1:
            self.head_ratio = config.attention_heads_list[layer_id]
            self.num_attention_heads = 1
        else:
            self.num_attention_heads = new_num_attention_heads
            self.head_ratio = config.head_ratio

        self.conv_kernel_size = config.similarity_list[layer_id]

        assert (
            config.hidden_dim_list[layer_id] % self.num_attention_heads == 0
        ), "hidden_size should be divisible by num_attention_heads"

        self.attention_head_size =  config.hidden_dim_list[layer_id] //config.attention_heads_list[layer_id]
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_dim_list[layer_id], self.all_head_size)
        self.key = nn.Linear(config.hidden_dim_list[layer_id], self.all_head_size)
        self.value = nn.Linear(config.hidden_dim_list[layer_id], self.all_head_size)

        self.key_conv_attn_layer = SeparableConv1D(
            config, config.hidden_dim_list[layer_id], self.all_head_size, self.conv_kernel_size
        )
        self.conv_kernel_layer = nn.Linear(self.all_head_size, self.num_attention_heads * self.conv_kernel_size)
        self.conv_out_layer = nn.Linear( config.hidden_dim_list[layer_id], self.all_head_size)

        self.unfold = nn.Unfold(
            kernel_size=[self.conv_kernel_size, 1], padding=[int((self.conv_kernel_size - 1) / 2), 0]
        )

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(2 * config.max_position_embeddings - 1, self.attention_head_size)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        output_attentions=False,
    ):
        mixed_query_layer = self.query(hidden_states)
        batch_size = hidden_states.size(0)
        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        if encoder_hidden_states is not None:
            mixed_key_layer = self.key(encoder_hidden_states)
            mixed_value_layer = self.value(encoder_hidden_states)
        else:
            mixed_key_layer = self.key(hidden_states)
            mixed_value_layer = self.value(hidden_states)

        mixed_key_conv_attn_layer = self.key_conv_attn_layer(hidden_states.transpose(1, 2))
        mixed_key_conv_attn_layer = mixed_key_conv_attn_layer.transpose(1, 2)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)
        conv_attn_layer = torch.multiply(mixed_key_conv_attn_layer, mixed_query_layer)

        conv_kernel_layer = self.conv_kernel_layer(conv_attn_layer)
        conv_kernel_layer = torch.reshape(conv_kernel_layer, [-1, self.conv_kernel_size, 1])
        conv_kernel_layer = torch.softmax(conv_kernel_layer, dim=1)

        conv_out_layer = self.conv_out_layer(hidden_states)
        conv_out_layer = torch.reshape(conv_out_layer, [batch_size, -1, self.all_head_size])
        conv_out_layer = conv_out_layer.transpose(1, 2).contiguous().unsqueeze(-1)
        conv_out_layer = nn.functional.unfold(
            conv_out_layer,
            kernel_size=[self.conv_kernel_size, 1],
            dilation=1,
            padding=[(self.conv_kernel_size - 1) // 2, 0],
            stride=1,
        )
        conv_out_layer = conv_out_layer.transpose(1, 2).reshape(
            batch_size, -1, self.all_head_size, self.conv_kernel_size
        )
        conv_out_layer = torch.reshape(conv_out_layer, [-1, self.attention_head_size, self.conv_kernel_size])
        conv_out_layer = torch.matmul(conv_out_layer, conv_kernel_layer)
        conv_out_layer = torch.reshape(conv_out_layer, [-1, self.all_head_size])

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        

        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key


        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in ConvBertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = torch.nn.functional.softmax(attention_scores, dim=-1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()

        conv_out = torch.reshape(conv_out_layer, [batch_size, -1, self.num_attention_heads, self.attention_head_size])
        context_layer = torch.cat([context_layer, conv_out], 2)

        new_context_layer_shape = context_layer.size()[:-2] + (self.head_ratio * self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)
        return outputs




class ConvBertAttentionModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()
        self.self = ConvBertSelfAttentionModular(config, layer_id)
        self.output = BertSelfOutputModular(config, layer_id)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        output_attentions=False,
    ):
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            output_attentions,
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


class GroupedLinearLayerModular(nn.Module):
    def __init__(self, input_size, output_size, num_groups):
        super().__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.num_groups = num_groups
        self.group_in_dim = self.input_size // self.num_groups
        self.group_out_dim = self.output_size // self.num_groups
        self.weight = nn.Parameter(torch.Tensor(self.num_groups, self.group_in_dim, self.group_out_dim))
        self.bias = nn.Parameter(torch.Tensor(output_size))

    def forward(self, hidden_states):
        batch_size = list(hidden_states.size())[0]
        x = torch.reshape(hidden_states, [-1, self.num_groups, self.group_in_dim])
        x = x.permute(1, 0, 2)
        x = torch.matmul(x, self.weight)
        x = x.permute(1, 0, 2)
        x = torch.reshape(x, [batch_size, -1, self.output_size])
        x = x + self.bias
        return x


class ConvBertIntermediateModular(nn.Module):
    def __init__(self, config, layer_id):
        super().__init__()
        if config.num_groups == 1:
            self.dense = nn.Linear(config.hidden_dim_list[layer_id], config.ff_dim_list[layer_id][0])
        else:
            self.dense = GroupedLinearLayer(
                input_size=config.hidden_dim_list[layer_id], output_size=config.ff_dim_list[layer_id][0], num_groups=config.num_groups
            )
        if isinstance(config.hidden_act, str):
            
            if config.hidden_act == "gelu":
                self.intermediate_act_fn = nn.GELU()

            else:
                self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

        
        modules = []
        
        modules.append(self.dense)
        modules.append(self.intermediate_act_fn)

        for i in range(len(config.ff_dim_list[layer_id])-1):

            if config.num_groups == 1:
                modules.append(nn.Linear(config.ff_dim_list[layer_id][i], config.ff_dim_list[layer_id][i+1]))
            else:
               modules.append(GroupedLinearLayer(
                input_size=config.ff_dim_list[layer_id][i], output_size=config.ff_dim_list[layer_id][i+1], num_groups=config.num_groups
            ))

            modules.append(self.intermediate_act_fn)

        self.sequential = nn.Sequential(*modules)

    def forward(self, hidden_states):
        
        return self.sequential(hidden_states)



class ConvBertOutputModular(nn.Module):
    def __init__(self, config, layer_id, last_layer):
        super().__init__()
        if config.num_groups == 1:
            self.dense = nn.Linear(config.ff_dim_list[layer_id][-1], config.hidden_dim_list[layer_id])
        else:
            self.dense = GroupedLinearLayer(
                input_size=config.ff_dim_list[layer_id][-1], output_size=config.hidden_dim_list[layer_id], num_groups=config.num_groups
            )
        self.LayerNorm = nn.LayerNorm(config.hidden_dim_list[layer_id], eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.last_layer = last_layer
        if last_layer:
                self.proj_required = False

        else:
            if  config.hidden_dim_list[layer_id]==config.hidden_dim_list[layer_id+1]:
                self.proj_required = False
            else:
                self.proj_required = True

        if self.proj_required:
            self.proj_head = nn.Linear(config.hidden_dim_list[layer_id],config.hidden_dim_list[layer_id+1])


    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        if self.proj_required:
            hidden_states = self.proj_head(hidden_states)
        return hidden_states


class ConvBertLayerModular(nn.Module):
    def __init__(self, config, layer_id, last_layer = False):
        super().__init__()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = ConvBertAttentionModular(config,layer_id)
        self.is_decoder = config.is_decoder
        self.add_cross_attention = config.add_cross_attention
        if self.add_cross_attention:
            assert self.is_decoder, f"{self} should be used as a decoder model if cross attention is added"
            self.crossattention = ConvBertAttentionModular(config,layer_id)
        self.intermediate = ConvBertIntermediateModular(config,layer_id)
        self.output = ConvBertOutputModular(config,layer_id,last_layer)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
        past_key_value=None,
    ):

        self_attention_outputs = self.attention(
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions=output_attentions,
        )
        attention_output = self_attention_outputs[0]
        outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        if self.is_decoder and encoder_hidden_states is not None:
            assert hasattr(
                self, "crossattention"
            ), f"If `encoder_hidden_states` are passed, {self} has to be instantiated with cross-attention layers by setting `config.add_cross_attention=True`"
            cross_attention_outputs = self.crossattention(
                attention_output,
                encoder_attention_mask,
                head_mask,
                encoder_hidden_states,
                output_attentions,
            )
            attention_output = cross_attention_outputs[0]
            outputs = outputs + cross_attention_outputs[1:]  # add cross attentions if we output attention weights

        layer_output = apply_chunking_to_forward(
            self.feed_forward_chunk, self.chunk_size_feed_forward, self.seq_len_dim, attention_output
        )
        outputs = (layer_output,) + outputs
        return outputs

    def feed_forward_chunk(self, attention_output):
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output

class BertEncoderModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        layer_list = []

        if config.from_model_dict_hetero:
            for layer_id in range(config.num_hidden_layers):
                layer_list.append(BertLayerModular(config,layer_id,layer_id == config.num_hidden_layers - 1))
        else:
            for layer_id in range(config.num_hidden_layers):
                if config.attention_type[layer_id] == 'c':
                    layer_list.append(ConvBertLayerModular(config,layer_id,layer_id == config.num_hidden_layers - 1))
                else:
                    layer_list.append(BertLayerModular(config,layer_id,layer_id == config.num_hidden_layers - 1))
        
        self.layer = nn.ModuleList(layer_list)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=True,
    ):
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None
        all_cross_attentions = () if output_attentions and self.config.add_cross_attention else None

        next_decoder_cache = () if use_cache else None
        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_head_mask = head_mask[i] if head_mask is not None else None
            past_key_value = past_key_values[i] if past_key_values is not None else None

            if getattr(self.config, "gradient_checkpointing", False) and self.training:

                if use_cache:
                    logger.warning(
                        "`use_cache=True` is incompatible with `config.gradient_checkpointing=True`. Setting "
                        "`use_cache=False`..."
                    )
                    use_cache = False

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs, past_key_value, output_attentions)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(layer_module),
                    hidden_states,
                    attention_mask,
                    layer_head_mask,
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                layer_outputs = layer_module(
                    hidden_states,
                    attention_mask,
                    layer_head_mask,
                    encoder_hidden_states,
                    encoder_attention_mask,
                    past_key_value,
                    output_attentions,
                )

            hidden_states = layer_outputs[0]
            if use_cache:
                next_decoder_cache += (layer_outputs[-1],)
            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_outputs[1],)
                if self.config.add_cross_attention:
                    all_cross_attentions = all_cross_attentions + (layer_outputs[2],)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    next_decoder_cache,
                    all_hidden_states,
                    all_self_attentions,
                    all_cross_attentions,
                ]
                if v is not None
            )
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=next_decoder_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )


class BertPoolerModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_dim_list[-1], config.hidden_dim_list[-1])
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertPredictionHeadTransformModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_dim_list[-1], config.hidden_dim_list[-1])
        if isinstance(config.hidden_act, str):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.hidden_act
        self.LayerNorm = nn.LayerNorm(config.hidden_dim_list[-1], eps=config.layer_norm_eps)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class BertLMPredictionHeadModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.transform = BertPredictionHeadTransformModular(config)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.

        self.decoder = nn.Linear(config.hidden_dim_list[-1], config.vocab_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))

        # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
        self.decoder.bias = self.bias
    
    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states)
        return hidden_states


class BertOnlyMLMHeadModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.predictions = BertLMPredictionHeadModular(config)

    def forward(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class BertOnlyNSPHeadModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.seq_relationship = nn.Linear(config.hidden_dim_list[-1], 2)

    def forward(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class BertPreTrainingHeadsModular(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.predictions = BertLMPredictionHeadModular(config)
        self.seq_relationship = nn.Linear(config.hidden_dim_list[-1], 2)

    def forward(self, sequence_output, pooled_output):
        prediction_scores = self.predictions(sequence_output)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


BERT_START_DOCSTRING = r"""

    This model inherits from :class:`~transformers.PreTrainedModel`. Check the superclass documentation for the generic
    methods the library implements for all its model (such as downloading or saving, resizing the input embeddings,
    pruning heads etc.)

    This model is also a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`__
    subclass. Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to
    general usage and behavior.

    Parameters:
        config (:class:`~transformers.BertConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model
            weights.
"""

BERT_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`({0})`):
            Indices of input sequence tokens in the vocabulary.

            Indices can be obtained using :class:`~transformers.BertTokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            details.

            `What are input IDs? <../glossary.html#input-ids>`__
        attention_mask (:obj:`torch.FloatTensor` of shape :obj:`({0})`, `optional`):
            Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        token_type_ids (:obj:`torch.LongTensor` of shape :obj:`({0})`, `optional`):
            Segment token indices to indicate first and second portions of the inputs. Indices are selected in ``[0,
            1]``:

            - 0 corresponds to a `sentence A` token,
            - 1 corresponds to a `sentence B` token.

            `What are token type IDs? <../glossary.html#token-type-ids>`_
        position_ids (:obj:`torch.LongTensor` of shape :obj:`({0})`, `optional`):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range ``[0,
            config.max_position_embeddings - 1]``.

            `What are position IDs? <../glossary.html#position-ids>`_
        head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
            Mask to nullify selected heads of the self-attention modules. Mask values selected in ``[0, 1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`({0}, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
            vectors than the model's internal embedding lookup matrix.
        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        return_dict (:obj:`bool`, `optional`):
            Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare Bert Model transformer outputting raw hidden-states without any specific head on top.",
    BERT_START_DOCSTRING,
    )

class BertModelModular(BertPreTrainedModel):
    """

    The model can behave as an encoder (with only self-attention) as well as a decoder, in which case a layer of
    cross-attention is added between the self-attention layers, following the architecture described in `Attention is
    all you need <https://arxiv.org/abs/1706.03762>`__ by Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit,
    Llion Jones, Aidan N. Gomez, Lukasz Kaiser and Illia Polosukhin.

    To behave as an decoder the model needs to be initialized with the :obj:`is_decoder` argument of the configuration
    set to :obj:`True`. To be used in a Seq2Seq model, the model needs to initialized with both :obj:`is_decoder`
    argument and :obj:`add_cross_attention` set to :obj:`True`; an :obj:`encoder_hidden_states` is then expected as an
    input to the forward pass.
    """

    def __init__(self, config, add_pooling_layer=True, transfer_mode='OD'):
        super().__init__(config)
        self.config = config

        self.embeddings = BertEmbeddingsModular(config)
        self.encoder = BertEncoderModular(config)
        self.pooler = BertPoolerModular(config) if add_pooling_layer else None

        assert transfer_mode in ['OD', 'RP'], '"transfer_mode" should be either ordered (OD) or random projection (RP)'
        self.transfer_mode = transfer_mode

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def load_model_from_source(self, source_model, debug=False):
        """
        Loads the BertModelModular from a source model. Updates weights of the layers uptil the hidden dimension matches. Also updates the weights 
        for matching feed forward dimensions.
        """
        count = 0
        total = len(self.embeddings.state_dict())+len(self.encoder.state_dict())

        source_config = source_model.config

        assert self.config.from_model_dict_hetero == source_config.from_model_dict_hetero, \
            'Source model should (not) have heterogeneous configuration'

        #Load embeddings if input size same, otherwise, using projection
        if self.config.hidden_dim_list[0] == source_config.hidden_dim_list[0]:
            if debug:
                print('Loading embeddings directly')

            self.embeddings.load_state_dict(source_model.embeddings.state_dict())
            count+=len(source_model.embeddings.state_dict())
        else:
            if debug:
                print(f'Transfering embeddings using mode: {self.transfer_mode}')

            lower_hidden_size = min(self.config.hidden_dim_list[0], source_config.hidden_dim_list[0])
            rp = random_projection.GaussianRandomProjection(lower_hidden_size)

            with torch.no_grad():
                self.embeddings.LayerNorm.weight[:lower_hidden_size] = source_model.embeddings.LayerNorm.weight[:lower_hidden_size]
                self.embeddings.LayerNorm.bias[:lower_hidden_size] = source_model.embeddings.LayerNorm.bias[:lower_hidden_size]
                
                if self.transfer_mode == 'OD':
                    self.embeddings.word_embeddings.weight[:, :lower_hidden_size] = source_model.embeddings.word_embeddings.weight[:, :lower_hidden_size]
                    self.embeddings.position_embeddings.weight[:, :lower_hidden_size] = source_model.embeddings.position_embeddings.weight[:, :lower_hidden_size]
                    self.embeddings.token_type_embeddings.weight[:, :lower_hidden_size] = source_model.embeddings.token_type_embeddings.weight[:, :lower_hidden_size]
                else:
                    self.embeddings.word_embeddings.weight[:, :lower_hidden_size] = nn.Parameter(torch.from_numpy(rp.fit_transform(
                        source_model.embeddings.word_embeddings.weight[:, :lower_hidden_size].cpu().numpy())))
                    self.embeddings.position_embeddings.weight[:, :lower_hidden_size] = nn.Parameter(torch.from_numpy(rp.fit_transform(
                        source_model.embeddings.position_embeddings.weight[:, :lower_hidden_size].cpu().numpy())))
                    self.embeddings.token_type_embeddings.weight[:, :lower_hidden_size] = nn.Parameter(torch.from_numpy(rp.fit_transform(
                        source_model.embeddings.token_type_embeddings.weight[:, :lower_hidden_size].cpu().numpy())))

        #Loading encoder
        if self.config.from_model_dict_hetero:
            with torch.no_grad():
                for i in range(min(self.config.num_hidden_layers,source_config.num_hidden_layers)):
                    if debug:
                        print(f'Checking layer {i}...')

                    if self.transfer_mode in ['OD', 'RP']:

                        attention_head_size = int(self.config.attention_heads_list[i][0].split('_')[2])
                        lower_all_head_size = min(self.encoder.layer[i].attention.self.query.weight.shape[1], 
                            source_model.encoder.layer[i].attention.self.query.weight.shape[1])
                        lower_attention_head_size = min(attention_head_size, int(source_config.attention_heads_list[i][0].split('_')[2]))
                        lower_hidden_size = min(self.config.hidden_dim_list[i], source_config.hidden_dim_list[i])

                        rp = random_projection.GaussianRandomProjection(lower_hidden_size)
                        rp_att = random_projection.GaussianRandomProjection(lower_attention_head_size)

                        self.encoder.layer[i].attention.self.dropout.load_state_dict(
                            source_model.encoder.layer[i].attention.self.dropout.state_dict())

                        if attention_head_size == int(source_config.attention_heads_list[i][0].split('_')[2]):
                            if debug:
                                print(f'\tLoading distace embeddings directly')

                            self.encoder.layer[i].attention.self.distance_embedding.load_state_dict(
                                source_model.encoder.layer[i].attention.self.distance_embedding.state_dict())
                        else:
                            if debug:
                                print(f'\tTransfering distance embeddings using mode: {self.transfer_mode}')

                            if self.transfer_mode == 'OD':
                                self.encoder.layer[i].attention.self.distance_embedding.weight[:, :lower_attention_head_size] = \
                                    source_model.encoder.layer[i].attention.self.distance_embedding.weight[:, :lower_attention_head_size]
                            else:
                                self.encoder.layer[i].attention.self.distance_embedding.weight[:, :lower_attention_head_size] = \
                                    nn.Parameter(torch.from_numpy(rp_att.fit_transform(
                                        source_model.encoder.layer[i].attention.self.distance_embedding.weight.cpu().numpy())))

                        curr_attn_types = [attention.split('_')[0] for attention in self.config.attention_heads_list[i]]
                        source_attn_types = [attention.split('_')[0] for attention in source_config.attention_heads_list[i]]

                        curr_sim_types = [attention.split('_')[1] for attention in self.config.attention_heads_list[i]]
                        source_sim_types = [attention.split('_')[1] for attention in source_config.attention_heads_list[i]]

                        curr_all_head_size = len(self.config.attention_heads_list[i]) * int(self.config.attention_heads_list[i][0].split('_')[2])
                        source_all_head_size = len(source_config.attention_heads_list[i]) * int(source_config.attention_heads_list[i][0].split('_')[2])

                        wma_count, conv_count = 0, 0
                        for j in range(min(len(self.config.attention_heads_list[i]), len(source_config.attention_heads_list[i]))):
                            # We only transfer attention weights if the corresponding head is the same
                            if curr_attn_types[j] == source_attn_types[j]:
                                if debug:
                                    print(f'\tTransfering attention head {j}: {self.config.attention_heads_list[i][j]}')

                                    self.encoder.layer[i].attention.self.query.bias[j*attention_head_size:(j+1)*attention_head_size] = \
                                        source_model.encoder.layer[i].attention.self.query.bias[j*attention_head_size:(j+1)*attention_head_size]
                                    self.encoder.layer[i].attention.self.key.bias[j*attention_head_size:(j+1)*attention_head_size] = \
                                        source_model.encoder.layer[i].attention.self.key.bias[j*attention_head_size:(j+1)*attention_head_size]
                                    self.encoder.layer[i].attention.self.value.bias[j*attention_head_size:(j+1)*attention_head_size] = \
                                        source_model.encoder.layer[i].attention.self.value.bias[j*attention_head_size:(j+1)*attention_head_size]

                                    if self.config.hidden_dim_list[i] != source_config.hidden_dim_list[i]:
                                        if self.transfer_mode == 'OD':
                                            self.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                source_model.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size]
                                            self.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                source_model.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size]
                                            self.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                source_model.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size]
                                        else:
                                            self.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                nn.Parameter(torch.from_numpy(rp.fit_transform(
                                                    source_model.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size].cpu().numpy())))
                                            self.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                nn.Parameter(torch.from_numpy(rp.fit_transform(
                                                    source_model.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size].cpu().numpy())))
                                            self.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size] = \
                                                nn.Parameter(torch.from_numpy(rp.fit_transform(
                                                    source_model.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :lower_hidden_size].cpu().numpy())))
                                    else:
                                        self.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :] = \
                                            source_model.encoder.layer[i].attention.self.query.weight[j*attention_head_size:(j+1)*attention_head_size, :]
                                        self.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :] = \
                                            source_model.encoder.layer[i].attention.self.key.weight[j*attention_head_size:(j+1)*attention_head_size, :]
                                        self.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :] = \
                                            source_model.encoder.layer[i].attention.self.value.weight[j*attention_head_size:(j+1)*attention_head_size, :]

                                if curr_sim_types[j] == 'wma' and source_sim_types[j] == 'wma':
                                    if attention_head_size == int(source_config.attention_heads_list[i][0].split('_')[2]):
                                        setattr(self.encoder.layer[i].attention.self, f'W{wma_count}', 
                                            getattr(source_model.encoder.layer[i].attention.self, f'W{wma_count}'))
                                    else:
                                        curr_w = getattr(self.encoder.layer[i].attention.self, f'W{wma_count}')
                                        source_w = getattr(source_model.encoder.layer[i].attention.self, f'W{wma_count}')

                                        if self.transfer_mode == 'OD':
                                            curr_w[:lower_attention_head_size, :lower_attention_head_size] = source[:lower_attention_head_size, :lower_attention_head_size]
                                        else:
                                            source_w = rp_att.fit_transform(source_w.cpu().numpy())
                                            source_w = rp_att.fit_transform(np.transpose(source_w))
                                            curr_w[:lower_attention_head_size, :lower_attention_head_size] = nn.Parameter(torch.from_nump(source_w))
                                    wma_count += 1
                                elif curr_sim_types[j].isnumeric():
                                    lower_sim_type = min(int(curr_sim_types[j]), int(source_sim_types[j]))

                                    curr_key_conv_attn_layer = getattr(self.encoder.layer[i].attention.self, f'key_conv_attn_layer{conv_count}')
                                    source_key_conv_attn_layer = getattr(source_model.encoder.layer[i].attention.self, f'key_conv_attn_layer{conv_count}')
                                    curr_conv_kernel_layer = getattr(self.encoder.layer[i].attention.self, f'conv_kernel_layer{conv_count}')
                                    source_conv_kernel_layer = getattr(source_model.encoder.layer[i].attention.self, f'conv_kernel_layer{conv_count}')
                                    curr_conv_out_layer = getattr(self.encoder.layer[i].attention.self, f'conv_out_layer{conv_count}')
                                    source_conv_out_layer = getattr(source_model.encoder.layer[i].attention.self, f'conv_out_layer{conv_count}')
                                    curr_unfold = getattr(self.encoder.layer[i].attention.self, f'unfold{conv_count}')
                                    source_unfold = getattr(source_model.encoder.layer[i].attention.self, f'unfold{conv_count}')
                                    
                                    curr_unfold.load_state_dict(source_unfold.state_dict())

                                    if attention_head_size == int(source_config.attention_heads_list[i][0].split('_')[2]) and int(curr_sim_types[j]) == int(source_sim_types[j]):
                                        curr_key_conv_attn_layer.load_state_dict(source_key_conv_attn_layer.state_dict())
                                        curr_conv_kernel_layer.load_state_dict(source_conv_kernel_layer.state_dict())
                                        curr_conv_out_layer.load_state_dict(source_conv_out_layer.state_dict())
                                    else:
                                        # TODO: Implement RP for convolutional layers
                                        curr_key_conv_attn_layer.bias[:lower_attention_head_size, :] = source_key_conv_attn_layer.bias[:lower_attention_head_size, :]
                                        curr_key_conv_attn_layer.depthwise.weight[:lower_attention_head_size, :, :lower_sim_type] = \
                                            torch.functional.F.interpolate(source_key_conv_attn_layer.depthwise.weight[:lower_attention_head_size, :, :], lower_sim_type)
                                        curr_key_conv_attn_layer.pointwise.weight[:lower_attention_head_size, :lower_attention_head_size] = \
                                            source_key_conv_attn_layer.pointwise.weight[:lower_attention_head_size, :lower_attention_head_size]

                                        curr_conv_kernel_layer.weight[:lower_sim_type, :lower_attention_head_size] = source_conv_kernel_layer.weight[:lower_sim_type, :lower_attention_head_size]
                                        curr_conv_kernel_layer.bias[:lower_sim_type] = source_conv_kernel_layer.bias[:lower_sim_type]

                                        curr_conv_out_layer.weight[:lower_attention_head_size, :lower_attention_head_size] = \
                                            source_conv_out_layer.weight[:lower_attention_head_size, :lower_attention_head_size]
                                        curr_conv_out_layer.bias[:lower_attention_head_size] = source_conv_out_layer.bias[:lower_attention_head_size]
                                    conv_count += 1

                            if curr_all_head_size == source_all_head_size and self.config.hidden_dim_list[i] == source_config.hidden_dim_list[i]:
                                self.encoder.layer[i].attention.output.load_state_dict(source_model.encoder.layer[i].attention.output.state_dict())
                            else:
                                self.encoder.layer[i].attention.output.dense.bias[:lower_hidden_size] = \
                                    source_model.encoder.layer[i].attention.output.dense.bias[:lower_hidden_size]

                                if self.config.hidden_dim_list[i] != source_config.hidden_dim_list[i]:
                                    if self.transfer_mode == 'OD':
                                        self.encoder.layer[i].attention.output.dense.weight[:lower_hidden_size, j*attention_head_size:(j+1)*attention_head_size] = \
                                            source_model.encoder.layer[i].attention.output.dense.weight[:lower_hidden_size, j*attention_head_size:(j+1)*attention_head_size]
                                    else:
                                        self.encoder.layer[i].attention.output.dense.weight[:lower_hidden_size, j*attention_head_size:(j+1)*attention_head_size] = \
                                            nn.Parameter(torch.from_numpy(np.transpose(rp.fit_transform(np.transpose(
                                                source_model.encoder.layer[i].attention.output.dense.weight[:lower_hidden_size, j*attention_head_size:(j+1)*attention_head_size].cpu().numpy())))))
                                else:
                                    self.encoder.layer[i].attention.output.dense.weight[:, j*attention_head_size:(j+1)*attention_head_size] = \
                                            source_model.encoder.layer[i].attention.output.dense.weight[:, j*attention_head_size:(j+1)*attention_head_size]

                                    assert self.encoder.layer[i].attention.output.dense.weight.shape[0] == lower_hidden_size

                        if self.config.hidden_dim_list[i] == source_config.hidden_dim_list[i]:
                            self.encoder.layer[i].attention.output.LayerNorm.load_state_dict(source_model.encoder.layer[i].attention.output.LayerNorm.state_dict())
                        else:
                            self.encoder.layer[i].attention.output.LayerNorm.weight[:lower_hidden_size] = \
                                source_model.encoder.layer[i].attention.output.LayerNorm.weight[:lower_hidden_size]
                            self.encoder.layer[i].attention.output.LayerNorm.bias[:lower_hidden_size] = \
                                source_model.encoder.layer[i].attention.output.LayerNorm.bias[:lower_hidden_size]
                        
                        self.encoder.layer[i].attention.output.dropout.load_state_dict(source_model.encoder.layer[i].attention.output.dropout.state_dict())

                        # Transfer weights of feed-forward layer(s)
                        for f in range(min(len(self.config.ff_dim_list[i]), len(source_config.ff_dim_list[i]))):
                            if debug:
                                print(f'\tTransfering feed-forward layer {f}')
                            lower_dim_0 = min(self.encoder.layer[i].intermediate.sequential[2*f].weight.shape[0],
                                source_model.encoder.layer[i].intermediate.sequential[2*f].weight.shape[0])
                            lower_dim_1 = min(self.encoder.layer[i].intermediate.sequential[2*f].weight.shape[1],
                                source_model.encoder.layer[i].intermediate.sequential[2*f].weight.shape[1])
                            self.encoder.layer[i].intermediate.sequential[2*f].weight[:lower_dim_0, :lower_dim_1] = \
                                source_model.encoder.layer[i].intermediate.sequential[2*f].weight[:lower_dim_0, :lower_dim_1]
                            self.encoder.layer[i].intermediate.sequential[2*f].bias[:lower_dim_0] = \
                                source_model.encoder.layer[i].intermediate.sequential[2*f].bias[:lower_dim_0]
                            
                        output_lower_dim = min(self.config.ff_dim_list[i][-1], source_config.ff_dim_list[i][-1])
                        self.encoder.layer[i].output.dense.bias[:lower_hidden_size] = source_model.encoder.layer[i].output.dense.bias[:lower_hidden_size]

                        if self.config.hidden_dim_list[i] != source_config.hidden_dim_list[i]:
                            if self.transfer_mode == 'OD':
                                self.encoder.layer[i].output.dense.weight[:lower_hidden_size, :output_lower_dim] = \
                                    source_model.encoder.layer[i].output.dense.weight[:lower_hidden_size, :output_lower_dim]
                            else:
                                self.encoder.layer[i].output.dense.weight[:lower_hidden_size, :output_lower_dim] = \
                                    nn.Parameter(torch.from_numpy(np.transpose(rp.fit_transform(
                                        np.transpose(source_model.encoder.layer[i].output.dense.weight[:lower_hidden_size, :output_lower_dim].cpu().numpy())))))
                        else:
                            self.encoder.layer[i].output.dense.weight[:, :output_lower_dim] = source_model.encoder.layer[i].output.dense.weight[:, :output_lower_dim]

                        if self.config.hidden_dim_list[i] == source_config.hidden_dim_list[i]:
                            self.encoder.layer[i].output.LayerNorm.load_state_dict(source_model.encoder.layer[i].output.LayerNorm.state_dict())
                        else:
                            self.encoder.layer[i].output.LayerNorm.weight[:lower_hidden_size] = source_model.encoder.layer[i].output.LayerNorm.weight[:lower_hidden_size]
                            self.encoder.layer[i].output.LayerNorm.bias[:lower_hidden_size] = source_model.encoder.layer[i].output.LayerNorm.bias[:lower_hidden_size]
                        
                        self.encoder.layer[i].output.dropout.load_state_dict(source_model.encoder.layer[i].output.dropout.state_dict())
        else:
            for i in range(min(self.config.num_hidden_layers,source_config.num_hidden_layers)):
                #Loading self attention 
                if self.config.attention_type[i] == source_config.attention_type[i] :
                    
                    if self.config.hidden_dim_list[i] ==  source_config.hidden_dim_list[i] and \
                    self.config.attention_heads_list[i] ==  source_config.attention_heads_list[i] and \
                    self.config.similarity_list[i] == source_config.similarity_list[i]:
                        
                        self.encoder.layer[i].attention.load_state_dict(source_model.encoder.layer[i].attention.state_dict())
                        count+=len(source_model.encoder.layer[i].attention.state_dict())

                        if self.config.ff_dim_list[i] == source_config.ff_dim_list[i] :
                            self.encoder.layer[i].intermediate.load_state_dict(source_model.encoder.layer[i].intermediate.state_dict())
                            count+=len(source_model.encoder.layer[i].intermediate.state_dict())
                            #print("Intermediate loaded")

                            if i + 1 < min(self.config.num_hidden_layers,source_config.num_hidden_layers) \
                                and self.config.hidden_dim_list[i+1] == source_config.hidden_dim_list[i+1]:
                                self.encoder.layer[i].output.load_state_dict(source_model.encoder.layer[i].output.state_dict())
                                count+=len(source_model.encoder.layer[i].output.state_dict())
                                #print("Output loaded")

                        #print("-"*3,"Loaded Weights for Layer:",i,"-"*3)
                else:
                    #print("-"*3,"Done Loading Source","-"*3)
                    break

        return count*1.0/total

            

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=BaseModelOutputWithPoolingAndCrossAttentions,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        encoder_hidden_states  (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden-states at the output of the last layer of the encoder. Used in the cross-attention if
            the model is configured as a decoder.
        encoder_attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on the padding token indices of the encoder input. This mask is used in
            the cross-attention if the model is configured as a decoder. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.
        past_key_values (:obj:`tuple(tuple(torch.FloatTensor))` of length :obj:`config.n_layers` with each tuple having 4 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden states of the attention blocks. Can be used to speed up decoding.

            If :obj:`past_key_values` are used, the user can optionally input only the last :obj:`decoder_input_ids`
            (those that don't have their past key value states given to this model) of shape :obj:`(batch_size, 1)`
            instead of all :obj:`decoder_input_ids` of shape :obj:`(batch_size, sequence_length)`.
        use_cache (:obj:`bool`, `optional`):
            If set to :obj:`True`, :obj:`past_key_values` key value states are returned and can be used to speed up
            decoding (see :obj:`past_key_values`).
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if self.config.is_decoder:
            use_cache = use_cache if use_cache is not None else self.config.use_cache
        else:
            use_cache = False

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
            batch_size, seq_length = input_shape
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
            batch_size, seq_length = input_shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        # past_key_values_length
        past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0

        if attention_mask is None:
            attention_mask = torch.ones(((batch_size, seq_length + past_key_values_length)), device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        extended_attention_mask: torch.Tensor = self.get_extended_attention_mask(attention_mask, input_shape, device)

        # If a 2D or 3D attention mask is provided for the cross-attention
        # we need to make broadcastable to [batch_size, num_heads, seq_length, seq_length]
        if self.config.is_decoder and encoder_hidden_states is not None:
            encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()
            encoder_hidden_shape = (encoder_batch_size, encoder_sequence_length)
            if encoder_attention_mask is None:
                encoder_attention_mask = torch.ones(encoder_hidden_shape, device=device)
            encoder_extended_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_extended_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)

        embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            inputs_embeds=inputs_embeds,
            past_key_values_length=past_key_values_length,
        )
        
        encoder_outputs = self.encoder(
            embedding_output,
            attention_mask=extended_attention_mask,
            head_mask=head_mask,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_extended_attention_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        sequence_output = encoder_outputs[0]
        pooled_output = self.pooler(sequence_output) if self.pooler is not None else None

        if not return_dict:
            return (sequence_output, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPoolingAndCrossAttentions(
            last_hidden_state=sequence_output,
            pooler_output=pooled_output,
            past_key_values=encoder_outputs.past_key_values,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
            cross_attentions=encoder_outputs.cross_attentions,
        )


@add_start_docstrings(
    """
    Bert Model with two heads on top as done during the pretraining: a `masked language modeling` head and a `next
    sentence prediction (classification)` head.
    """,
    BERT_START_DOCSTRING,
)
class BertForPreTrainingModular(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.bert = BertModelModular(config)
        self.cls = BertPreTrainingHeadsModular(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.cls.predictions.decoder

    def set_output_embeddings(self, new_embeddings):
        print("Set decoder to new values")
        self.cls.predictions.decoder = new_embeddings

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @replace_return_docstrings(output_type=BertForPreTrainingOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        next_sentence_label=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape ``(batch_size, sequence_length)``, `optional`):
            Labels for computing the masked language modeling loss. Indices should be in ``[-100, 0, ...,
            config.vocab_size]`` (see ``input_ids`` docstring) Tokens with indices set to ``-100`` are ignored
            (masked), the loss is only computed for the tokens with labels in ``[0, ..., config.vocab_size]``
        next_sentence_label (``torch.LongTensor`` of shape ``(batch_size,)``, `optional`):
            Labels for computing the next sequence prediction (classification) loss. Input should be a sequence pair
            (see :obj:`input_ids` docstring) Indices should be in ``[0, 1]``:

            - 0 indicates sequence B is a continuation of sequence A,
            - 1 indicates sequence B is a random sequence.
        kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
            Used to hide legacy arguments that have been deprecated.

        Returns:

        Example::

            >>> from transformers import BertTokenizer, BertForPreTrainingModular
            >>> import torch

            >>> tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
            >>> model = BertForPreTraining.from_pretrained('bert-base-uncased')

            >>> inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")
            >>> outputs = model(**inputs)

            >>> prediction_logits = outputs.prediction_logits
            >>> seq_relationship_logits = outputs.seq_relationship_logits
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output, pooled_output = outputs[:2]
        prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output)

        total_loss = None
        if labels is not None and next_sentence_label is not None:
            loss_fct = CrossEntropyLoss()
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), labels.view(-1))
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            total_loss = masked_lm_loss + next_sentence_loss

        if not return_dict:
            output = (prediction_scores, seq_relationship_score) + outputs[2:]
            return ((total_loss,) + output) if total_loss is not None else output

        return BertForPreTrainingOutput(
            loss=total_loss,
            prediction_logits=prediction_scores,
            seq_relationship_logits=seq_relationship_score,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@add_start_docstrings(
    """Bert Model with a `language modeling` head on top for CLM fine-tuning. """, BERT_START_DOCSTRING
)
class BertLMHeadModelModular(BertPreTrainedModel):

    _keys_to_ignore_on_load_unexpected = [r"pooler"]
    _keys_to_ignore_on_load_missing = [r"position_ids", r"predictions.decoder.bias"]

    def __init__(self, config):
        super().__init__(config)

        if not config.is_decoder:
            logger.warning("If you want to use `BertLMHeadModel` as a standalone, add `is_decoder=True.`")

        self.bert = BertModelModular(config, add_pooling_layer=False)
        self.cls = BertOnlyMLMHeadModular(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.cls.predictions.decoder

    def set_output_embeddings(self, new_embeddings):
        self.cls.predictions.decoder = new_embeddings

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @replace_return_docstrings(output_type=CausalLMOutputWithCrossAttentions, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        labels=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        encoder_hidden_states  (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden-states at the output of the last layer of the encoder. Used in the cross-attention if
            the model is configured as a decoder.
        encoder_attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on the padding token indices of the encoder input. This mask is used in
            the cross-attention if the model is configured as a decoder. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for computing the left-to-right language modeling loss (next word prediction). Indices should be in
            ``[-100, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring) Tokens with indices set to ``-100`` are
            ignored (masked), the loss is only computed for the tokens with labels n ``[0, ..., config.vocab_size]``
        past_key_values (:obj:`tuple(tuple(torch.FloatTensor))` of length :obj:`config.n_layers` with each tuple having 4 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden states of the attention blocks. Can be used to speed up decoding.

            If :obj:`past_key_values` are used, the user can optionally input only the last :obj:`decoder_input_ids`
            (those that don't have their past key value states given to this model) of shape :obj:`(batch_size, 1)`
            instead of all :obj:`decoder_input_ids` of shape :obj:`(batch_size, sequence_length)`.
        use_cache (:obj:`bool`, `optional`):
            If set to :obj:`True`, :obj:`past_key_values` key value states are returned and can be used to speed up
            decoding (see :obj:`past_key_values`).

        Returns:

        Example::

            >>> from transformers import BertTokenizer, BertLMHeadModel, BertConfig
            >>> import torch

            >>> tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
            >>> config = BertConfig.from_pretrained("bert-base-cased")
            >>> config.is_decoder = True
            >>> model = BertLMHeadModel.from_pretrained('bert-base-cased', config=config)

            >>> inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")
            >>> outputs = model(**inputs)

            >>> prediction_logits = outputs.logits
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        if labels is not None:
            use_cache = False

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]
        prediction_scores = self.cls(sequence_output)

        lm_loss = None
        if labels is not None:
            # we are doing next-token prediction; shift prediction scores and input ids by one
            shifted_prediction_scores = prediction_scores[:, :-1, :].contiguous()
            labels = labels[:, 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shifted_prediction_scores.view(-1, self.config.vocab_size), labels.view(-1))

        if not return_dict:
            output = (prediction_scores,) + outputs[2:]
            return ((lm_loss,) + output) if lm_loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=lm_loss,
            logits=prediction_scores,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            cross_attentions=outputs.cross_attentions,
        )

    def prepare_inputs_for_generation(self, input_ids, past=None, attention_mask=None, **model_kwargs):
        input_shape = input_ids.shape
        # if model is used as a decoder in encoder-decoder model, the decoder attention mask is created on the fly
        if attention_mask is None:
            attention_mask = input_ids.new_ones(input_shape)

        # cut decoder_input_ids if past is used
        if past is not None:
            input_ids = input_ids[:, -1:]

        return {"input_ids": input_ids, "attention_mask": attention_mask, "past_key_values": past}

    def _reorder_cache(self, past, beam_idx):
        reordered_past = ()
        for layer_past in past:
            reordered_past += (tuple(past_state.index_select(0, beam_idx) for past_state in layer_past),)
        return reordered_past


@add_start_docstrings("""Bert Model with a `language modeling` head on top. """, BERT_START_DOCSTRING)
class BertForMaskedLMModular(BertPreTrainedModel):

    _keys_to_ignore_on_load_unexpected = [r"pooler"]
    _keys_to_ignore_on_load_missing = [r"position_ids", r"predictions.decoder.bias"]

    def __init__(self, config, transfer_mode='OD'):
        super().__init__(config)

        if config.is_decoder:
            logger.warning(
                "If you want to use `BertForMaskedLM` make sure `config.is_decoder=False` for "
                "bi-directional self-attention."
            )

        assert transfer_mode in ['OD', 'RP'], '"transfer_mode" should be either ordered (OD) or random projection (RP)'
        self.transfer_mode = transfer_mode

        self.bert = BertModelModular(config, add_pooling_layer=False, transfer_mode=self.transfer_mode)
        self.cls = BertOnlyMLMHeadModular(config)

        self.init_weights()

    def load_model_from_source(self, source_model, debug=False):
        initial_state_dict = copy.deepcopy(self.state_dict())
        
        # Transfer weights
        self.bert.load_model_from_source(source_model.bert, debug)
        if debug:
            print(f'Transfering MLM head\n')
        self.cls.load_state_dict(source_model.cls.state_dict())

        # Get ratio of transfer of weights
        not_transferred_weights, not_transferred_weights_sum, total_weights = 0, 0, 0
        for key, value in self.state_dict().items():
            not_transferred_weights = torch.sum(torch.eq(value, initial_state_dict[key]))
            not_transferred_weights_sum += not_transferred_weights
            total_weights += torch.prod(torch.tensor(value.shape))
            if debug:
                if not_transferred_weights != 0: print(f'Model key: {key} is not transferred (or has {not_transferred_weights} same weights)')
                else:
                    print(f'Model key: {key} is transferred successfully!')

        # Return weight transfer ratio
        return (1 - not_transferred_weights_sum/total_weights)


    def get_output_embeddings(self):
        return self.cls.predictions.decoder

    def set_output_embeddings(self, new_embeddings):
        self.cls.predictions.decoder = new_embeddings

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=MaskedLMOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for computing the masked language modeling loss. Indices should be in ``[-100, 0, ...,
            config.vocab_size]`` (see ``input_ids`` docstring) Tokens with indices set to ``-100`` are ignored
            (masked), the loss is only computed for the tokens with labels in ``[0, ..., config.vocab_size]``
        """

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]
        prediction_scores = self.cls(sequence_output)

        masked_lm_loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()  # -100 index = padding token
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), labels.view(-1))

        if not return_dict:
            output = (prediction_scores,) + outputs[2:]
            return ((masked_lm_loss,) + output) if masked_lm_loss is not None else output

        return MaskedLMOutput(
            loss=masked_lm_loss,
            logits=prediction_scores,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(self, input_ids, attention_mask=None, **model_kwargs):
        input_shape = input_ids.shape
        effective_batch_size = input_shape[0]

        #  add a dummy token
        assert self.config.pad_token_id is not None, "The PAD token should be defined for generation"
        attention_mask = torch.cat([attention_mask, attention_mask.new_zeros((attention_mask.shape[0], 1))], dim=-1)
        dummy_token = torch.full(
            (effective_batch_size, 1), self.config.pad_token_id, dtype=torch.long, device=input_ids.device
        )
        input_ids = torch.cat([input_ids, dummy_token], dim=1)

        return {"input_ids": input_ids, "attention_mask": attention_mask}


@add_start_docstrings(
    """Bert Model with a `next sentence prediction (classification)` head on top. """,
    BERT_START_DOCSTRING,
)
class BertForNextSentencePredictionModular(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.bert = BertModelModular(config)
        self.cls = BertOnlyNSPHeadModular(config)

        self.init_weights()

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @replace_return_docstrings(output_type=NextSentencePredictorOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for computing the next sequence prediction (classification) loss. Input should be a sequence pair
            (see ``input_ids`` docstring). Indices should be in ``[0, 1]``:

            - 0 indicates sequence B is a continuation of sequence A,
            - 1 indicates sequence B is a random sequence.

        Returns:

        Example::

            >>> from transformers import BertTokenizer, BertForNextSentencePrediction
            >>> import torch

            >>> tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
            >>> model = BertForNextSentencePrediction.from_pretrained('bert-base-uncased')

            >>> prompt = "In Italy, pizza served in formal settings, such as at a restaurant, is presented unsliced."
            >>> next_sentence = "The sky is blue due to the shorter wavelength of blue light."
            >>> encoding = tokenizer(prompt, next_sentence, return_tensors='pt')

            >>> outputs = model(**encoding, labels=torch.LongTensor([1]))
            >>> logits = outputs.logits
            >>> assert logits[0, 0] < logits[0, 1] # next sentence was random
        """

        if "next_sentence_label" in kwargs:
            warnings.warn(
                "The `next_sentence_label` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("next_sentence_label")

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        pooled_output = outputs[1]

        seq_relationship_scores = self.cls(pooled_output)

        next_sentence_loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            next_sentence_loss = loss_fct(seq_relationship_scores.view(-1, 2), labels.view(-1))

        if not return_dict:
            output = (seq_relationship_scores,) + outputs[2:]
            return ((next_sentence_loss,) + output) if next_sentence_loss is not None else output

        return NextSentencePredictorOutput(
            loss=next_sentence_loss,
            logits=seq_relationship_scores,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@add_start_docstrings(
    """
    Bert Model transformer with a sequence classification/regression head on top (a linear layer on top of the pooled
    output) e.g. for GLUE tasks.
    """,
    BERT_START_DOCSTRING,
)
class BertForSequenceClassificationModular(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.bert = BertModelModular(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_dim_list[-1], config.num_labels)

        self.init_weights()

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=SequenceClassifierOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for computing the sequence classification/regression loss. Indices should be in :obj:`[0, ...,
            config.num_labels - 1]`. If :obj:`config.num_labels == 1` a regression loss is computed (Mean-Square loss),
            If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        loss = None
        if labels is not None:
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
                
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))

        
        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@add_start_docstrings(
    """
    Bert Model with a multiple choice classification head on top (a linear layer on top of the pooled output and a
    softmax) e.g. for RocStories/SWAG tasks.
    """,
    BERT_START_DOCSTRING,
)
class BertForMultipleChoiceModular(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.bert = BertModelModular(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_dim_list[-1], 1)

        self.init_weights()

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, num_choices, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=MultipleChoiceModelOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for computing the multiple choice classification loss. Indices should be in ``[0, ...,
            num_choices-1]`` where :obj:`num_choices` is the size of the second dimension of the input tensors. (See
            :obj:`input_ids` above)
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        num_choices = input_ids.shape[1] if input_ids is not None else inputs_embeds.shape[1]

        input_ids = input_ids.view(-1, input_ids.size(-1)) if input_ids is not None else None
        attention_mask = attention_mask.view(-1, attention_mask.size(-1)) if attention_mask is not None else None
        token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1)) if token_type_ids is not None else None
        position_ids = position_ids.view(-1, position_ids.size(-1)) if position_ids is not None else None
        inputs_embeds = (
            inputs_embeds.view(-1, inputs_embeds.size(-2), inputs_embeds.size(-1))
            if inputs_embeds is not None
            else None
        )

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, num_choices)

        loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)

        if not return_dict:
            output = (reshaped_logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return MultipleChoiceModelOutput(
            loss=loss,
            logits=reshaped_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@add_start_docstrings(
    """
    Bert Model with a token classification head on top (a linear layer on top of the hidden-states output) e.g. for
    Named-Entity-Recognition (NER) tasks.
    """,
    BERT_START_DOCSTRING,
)
class BertForTokenClassificationModular(BertPreTrainedModel):

    _keys_to_ignore_on_load_unexpected = [r"pooler"]

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.bert = BertModelModular(config, add_pooling_layer=False)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_dim_list[-1], config.num_labels)

        self.init_weights()

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=TokenClassifierOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for computing the token classification loss. Indices should be in ``[0, ..., config.num_labels -
            1]``.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)
                active_labels = torch.where(
                    active_loss, labels.view(-1), torch.tensor(loss_fct.ignore_index).type_as(labels)
                )
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return TokenClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@add_start_docstrings(
    """
    Bert Model with a span classification head on top for extractive question-answering tasks like SQuAD (a linear
    layers on top of the hidden-states output to compute `span start logits` and `span end logits`).
    """,
    BERT_START_DOCSTRING,
)
class BertForQuestionAnsweringModular(BertPreTrainedModel):

    _keys_to_ignore_on_load_unexpected = [r"pooler"]

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.bert = BertModelModular(config, add_pooling_layer=False)
        self.qa_outputs = nn.Linear(config.hidden_dim_list[-1], config.num_labels)

        self.init_weights()

    @add_start_docstrings_to_model_forward(BERT_INPUTS_DOCSTRING.format("batch_size, sequence_length"))
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=QuestionAnsweringModelOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        start_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the start of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (:obj:`sequence_length`). Position outside of the
            sequence are not taken into account for computing the loss.
        end_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the end of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (:obj:`sequence_length`). Position outside of the
            sequence are not taken into account for computing the loss.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]

        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        total_loss = None
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2

        if not return_dict:
            output = (start_logits, end_logits) + outputs[2:]
            return ((total_loss,) + output) if total_loss is not None else output

        return QuestionAnsweringModelOutput(
            loss=total_loss,
            start_logits=start_logits,
            end_logits=end_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
