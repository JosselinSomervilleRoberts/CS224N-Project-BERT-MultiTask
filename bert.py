from typing import Dict, List, Optional, Union, Tuple, Callable
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from base_bert import BertPreTrainedModel
from utils import *


class BertSelfAttention(nn.Module):
  def __init__(self, config, init_to_identity=False):
    super().__init__()

    self.num_attention_heads = config.num_attention_heads
    self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
    self.all_head_size = self.num_attention_heads * self.attention_head_size

    # initialize the linear transformation layers for key, value, query
    self.query = nn.Linear(config.hidden_size, self.all_head_size)
    self.key = nn.Linear(config.hidden_size, self.all_head_size)
    self.value = nn.Linear(config.hidden_size, self.all_head_size)
    # this dropout is applied to normalized attention scores following the original implementation of transformer
    # although it is a bit unusual, we empirically observe that it yields better performance
    self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    # Intialize the weight of query, key, value such that the self-attention is the identity function
    if init_to_identity:
      # initialize the linear transformation layers for key, value, query
      self.query.weight.data.zero_()
      self.query.bias.data.fill_(1.0 / math.sqrt(self.attention_head_size))

      self.key.weight.data.zero_()
      self.key.bias.data.fill_(1.0 / math.sqrt(self.attention_head_size))

      self.value.weight.data.zero_()
      self.value.bias.data.fill_(1.0 / math.sqrt(self.attention_head_size))

  def transform(self, x, linear_layer):
    # the corresponding linear_layer of k, v, q are used to project the hidden_state (x)
    bs, seq_len = x.shape[:2]
    proj = linear_layer(x)
    # next, we need to produce multiple heads for the proj 
    # this is done by spliting the hidden state to self.num_attention_heads, each of size self.attention_head_size
    proj = proj.view(bs, seq_len, self.num_attention_heads, self.attention_head_size)
    # by proper transpose, we have proj of [bs, num_attention_heads, seq_len, attention_head_size]
    proj = proj.transpose(1, 2)
    return proj

  def attention(self, key, query, value, attention_mask):
    '''This function calculates the multi-head attention following the original implementation of transformer.'''
    # each attention is calculated following eq (1) of https://arxiv.org/pdf/1706.03762.pdf
    # attention scores are calculated by multiply query and key 
    # and get back a score matrix S of [bs, num_attention_heads, seq_len, seq_len]
    # S[*, i, j, k] represents the (unnormalized)attention score between the j-th and k-th token, given by i-th attention head
    # before normalizing the scores, use the attention mask to mask out the padding token scores
    # Note again: in the attention_mask non-padding tokens with 0 and padding tokens with a large negative number 

    # normalize the scores
    # multiply the attention scores to the value and get back V'
    # next, we need to concat multi-heads and recover the original shape [bs, seq_len, num_attention_heads * attention_head_size = hidden_size]

    attention_score = torch.matmul(query, key.transpose(-1, -2))
    attention_score = attention_score + attention_mask
    attention_score = attention_score / math.sqrt(self.attention_head_size)
    attention_score = F.softmax(attention_score, dim=-1)
    attention_score = self.dropout(attention_score)
    attention_value = torch.matmul(attention_score, value)
    attention_value = attention_value.transpose(1, 2).contiguous()
    attention_value = attention_value.view(attention_value.shape[0], attention_value.shape[1], self.all_head_size)
    return attention_value


  def forward(self, hidden_states, attention_mask):
    """
    hidden_states: [bs, seq_len, hidden_state]
    attention_mask: [bs, 1, 1, seq_len]
    output: [bs, seq_len, hidden_state]
    """
    # first, we have to generate the key, value, query for each token for multi-head attention w/ transform (more details inside the function)
    # of *_layers are of [bs, num_attention_heads, seq_len, attention_head_size]
    key_layer = self.transform(hidden_states, self.key)
    value_layer = self.transform(hidden_states, self.value)
    query_layer = self.transform(hidden_states, self.query)
    # calculate the multi-head attention 
    attn_value = self.attention(key_layer, query_layer, value_layer, attention_mask)
    return attn_value


# Implement a Bert Self-Attention Layer with Projected Attention Layer (PAL)
# This adds a low rank attention mechanism per task to the original BERT self-attention layer

class TaskSpecificAttention(nn.Module):
  def __init__(self, config, project_up = None, project_down = None, perform_initial_init=False):
    super().__init__()
    #print("Creating project down layer with hidden size", config.hidden_size, "and low rank size", config.low_rank_size)
    self.project_down = nn.Linear(config.hidden_size, config.low_rank_size) if project_down is None else project_down
    self.project_up = nn.Linear(config.low_rank_size, config.hidden_size) if project_up is None else project_up
    config_self_attention = copy.deepcopy(config)
    config_self_attention.hidden_size = config.low_rank_size
    # config_self_attention.num_attention_heads = 6
    self.attention = BertSelfAttention(config_self_attention, init_to_identity=perform_initial_init)

    # Intialize the weight of project_down, project_up such that the self-attention is the zero function
    if perform_initial_init:
      if project_down is None:
        self.project_down.weight.data.zero_()
        self.project_down.bias.data.zero_()
      if project_up is None:
        self.project_up.weight.data.zero_()
        self.project_up.bias.data.zero_()

  def forward(self, hidden_states, attention_mask):
    """
    hidden_states: [bs, seq_len, hidden_state]
    attention_mask: [bs, 1, 1, seq_len]
    output: [bs, seq_len, hidden_state]
    """
    # Step 1: project to a lower rank space
    #print("Project down shape", self.project_down
    #print("hidden_states", hidden_states.shape)
    low_rank_hidden_states = self.project_down(hidden_states)
    #print("low_rank_hidden_states", low_rank_hidden_states.shape)
    low_rank_attention_mask = attention_mask

    # Step 2: apply the original BERT self-attention layer
    attn_value = self.attention(low_rank_hidden_states, low_rank_attention_mask)
    #print("attn_value", attn_value.shape)

    # Step 3: project back to the original hidden size
    attn_value = self.project_up(attn_value)

    return attn_value


class BertLayer(nn.Module):
  def __init__(self, config):
    super().__init__()
    # multi-head attention
    self.self_attention = BertSelfAttention(config)
    # add-norm
    self.attention_dense = nn.Linear(config.hidden_size, config.hidden_size)
    self.attention_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.attention_dropout = nn.Dropout(config.hidden_dropout_prob)
    # feed forward
    self.interm_dense = nn.Linear(config.hidden_size, config.intermediate_size)
    self.interm_af = F.gelu
    # another add-norm
    self.out_dense = nn.Linear(config.intermediate_size, config.hidden_size)
    self.out_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.out_dropout = nn.Dropout(config.hidden_dropout_prob)

  def add_norm(self, input, output, dense_layer, dropout, ln_layer):
    """
    this function is applied after the multi-head attention layer or the feed forward layer
    input: the input of the previous layer
    output: the output of the previous layer
    dense_layer: used to transform the output
    dropout: the dropout to be applied 
    ln_layer: the layer norm to be applied
    """
    # Hint: Remember that BERT applies to the output of each sub-layer, before it is added to the sub-layer input and normalized
    x = input + dense_layer(dropout(output))
    x = ln_layer(x)
    return x



  def forward(self, hidden_states, attention_mask):
    """
    hidden_states: either from the embedding layer (first bert layer) or from the previous bert layer
    as shown in the left of Figure 1 of https://arxiv.org/pdf/1706.03762.pdf 
    each block consists of 
    1. a multi-head attention layer (BertSelfAttention)
    2. a add-norm that takes the input and output of the multi-head attention layer
    3. a feed forward layer
    4. a add-norm that takes the input and output of the feed forward layer
    """
    self_attention_output = self.self_attention(hidden_states, attention_mask)
    self_attention_output = self.add_norm(hidden_states, self_attention_output, self.attention_dense, self.attention_dropout, self.attention_layer_norm)
    interm_output = self.interm_af(self.interm_dense(self_attention_output))
    output = self.add_norm(self_attention_output, interm_output, self.out_dense, self.out_dropout, self.out_layer_norm)
    return output


class BertLayerWithPAL(BertLayer):
  def __init__(self, config, project_ups = None, project_downs = None):
    super().__init__(config)
    
    # Task-specific attention
    self.project_ups = nn.ModuleList([nn.Linear(config.low_rank_size, config.hidden_size) for task in range(config.num_tasks)]) if project_ups is None else project_ups
    self.project_downs = nn.ModuleList([nn.Linear(config.hidden_size, config.low_rank_size) for task in range(config.num_tasks)]) if project_downs is None else project_downs
    self.task_attention = nn.ModuleList([TaskSpecificAttention(config, project_up=self.project_ups[task], project_down=self.project_downs[task]) for task in range(config.num_tasks)])


  def forward(self, hidden_states, attention_mask, task_id):
    """
    hidden_states: [bs, seq_len, hidden_state]
    attention_mask: [bs, 1, 1, seq_len]
    task_id: int
    output: [bs, seq_len, hidden_state]
    """
    self_attention_output = self.self_attention(hidden_states, attention_mask)
    task_attention_output = self.task_attention[task_id](hidden_states, attention_mask)
    attention_output = self_attention_output + task_attention_output
    self_attention_output = self.add_norm(hidden_states, attention_output, self.attention_dense, self.attention_dropout, self.attention_layer_norm)
    interm_output = self.interm_af(self.interm_dense(self_attention_output))
    output = self.add_norm(self_attention_output, interm_output, self.out_dense, self.out_dropout, self.out_layer_norm)
    #print("output", output.shape)
    return output

  def from_BertLayer(bert_layer, config, project_ups = None, project_downs = None, train_pal = True):
    """
    this function is used to convert a BertLayer to BertLayerWithPAL
    bert_layer: BertLayer
    config: BertConfig
    output: BertLayerWithPAL
    """
    # Hint: you can use the following code to convert a BertLayer to BertLayerWithPAL
    # pal_layer = BertLayerWithPAL.from_BertLayer(bert_layer, config)
    bert_layer.__class__ = BertLayerWithPAL
    #print(config.low_rank_size)
    bert_layer.task_attention = nn.ModuleList([TaskSpecificAttention(config, project_up=project_ups[task], project_down=project_downs[task], perform_initial_init=True) for task in range(config.num_tasks)])
    
    for param in bert_layer.task_attention.parameters():
      param.requires_grad = train_pal

    return bert_layer



class BertModel(BertPreTrainedModel):
  """
  the bert model returns the final embeddings for each token in a sentence
  it consists
  1. embedding (used in self.embed)
  2. a stack of n bert layers (used in self.encode)
  3. a linear transformation layer for [CLS] token (used in self.forward, as given)
  """
  def __init__(self, config, bert_layer=BertLayer):
    super().__init__(config)
    self.config = config

    # embedding
    self.word_embedding = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
    self.pos_embedding = nn.Embedding(config.max_position_embeddings, config.hidden_size)
    self.tk_type_embedding = nn.Embedding(config.type_vocab_size, config.hidden_size)
    self.embed_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.embed_dropout = nn.Dropout(config.hidden_dropout_prob)
    # position_ids (1, len position emb) is a constant, register to buffer
    position_ids = torch.arange(config.max_position_embeddings).unsqueeze(0)
    self.register_buffer('position_ids', position_ids)

    # bert encoder
    self.bert_layers = nn.ModuleList([bert_layer(config) for _ in range(config.num_hidden_layers)])

    # for [CLS] token
    self.pooler_dense = nn.Linear(config.hidden_size, config.hidden_size)
    self.pooler_af = nn.Tanh()

    self.init_weights()

  def embed(self, input_ids):
    input_shape = input_ids.size()
    seq_length = input_shape[1]

    # Get word embedding from self.word_embedding into input_embeds.
    inputs_embeds = self.word_embedding(input_ids)

    # Get position index and position embedding from self.pos_embedding into pos_embeds.
    pos_ids = self.position_ids[:, :seq_length]
    pos_embeds = self.pos_embedding(pos_ids)

    # Get token type ids, since we are not consider token type, just a placeholder.
    tk_type_ids = torch.zeros(input_shape, dtype=torch.long, device=input_ids.device)
    tk_type_embeds = self.tk_type_embedding(tk_type_ids)

    # Add three embeddings together; then apply embed_layer_norm and dropout and return.
    embeds = inputs_embeds + pos_embeds + tk_type_embeds
    embeds = self.embed_layer_norm(embeds)
    embeds = self.embed_dropout(embeds)
    return embeds


  def encode(self, hidden_states, attention_mask):
    """
    hidden_states: the output from the embedding layer [batch_size, seq_len, hidden_size]
    attention_mask: [batch_size, seq_len]
    """
    # get the extended attention mask for self attention
    # returns extended_attention_mask of [batch_size, 1, 1, seq_len]
    # non-padding tokens with 0 and padding tokens with a large negative number 
    extended_attention_mask: torch.Tensor = get_extended_attention_mask(attention_mask, self.dtype)

    # pass the hidden states through the encoder layers
    for i, layer_module in enumerate(self.bert_layers):
      # feed the encoding from the last bert_layer to the next
      hidden_states = layer_module(hidden_states, extended_attention_mask)

    return hidden_states

  def forward(self, input_ids, attention_mask):
    """
    input_ids: [batch_size, seq_len], seq_len is the max length of the batch
    attention_mask: same size as input_ids, 1 represents non-padding tokens, 0 represents padding tokens
    """
    # get the embedding for each input token
    embedding_output = self.embed(input_ids=input_ids)

    # feed to a transformer (a stack of BertLayers)
    sequence_output = self.encode(embedding_output, attention_mask=attention_mask)

    # get cls token hidden state
    first_tk = sequence_output[:, 0]
    first_tk = self.pooler_dense(first_tk)
    first_tk = self.pooler_af(first_tk)

    return {'last_hidden_state': sequence_output, 'pooler_output': first_tk}


class BertModelWithPAL(BertModel):
  def __init__(self, config):
    super().__init__(config, bert_layer=BertLayerWithPAL)

  def from_BertModel(bert_model, bert_config, train_pal=True):
    bert_model.__class__ = BertModelWithPAL
    bert_model.project_ups = nn.ModuleList([nn.Linear(bert_config.low_rank_size, bert_config.hidden_size) for task in range(bert_config.num_tasks)])
    bert_model.project_downs = nn.ModuleList([nn.Linear(bert_config.hidden_size, bert_config.low_rank_size) for task in range(bert_config.num_tasks)])
    # Intialize the low rank matrices to zero.
    for project_up in bert_model.project_ups:
      nn.init.zeros_(project_up.weight)
    for project_down in bert_model.project_downs:
      nn.init.zeros_(project_down.weight)
    bert_model.bert_layers = nn.ModuleList([BertLayerWithPAL.from_BertLayer(bert_layer, bert_config, project_ups=bert_model.project_ups, project_downs=bert_model.project_downs, train_pal=train_pal) for bert_layer in bert_model.bert_layers])
    for param in bert_model.project_downs.parameters():
      param.requires_grad = train_pal
    for param in bert_model.project_ups.parameters():
      param.requires_grad = train_pal


  def encode(self, hidden_states, attention_mask, task_id):
    """
    hidden_states: the output from the embedding layer [batch_size, seq_len, hidden_size]
    attention_mask: [batch_size, seq_len]
    """
    # get the extended attention mask for self attention
    # returns extended_attention_mask of [batch_size, 1, 1, seq_len]
    # non-padding tokens with 0 and padding tokens with a large negative number 
    extended_attention_mask: torch.Tensor = get_extended_attention_mask(attention_mask, self.dtype)

    # pass the hidden states through the encoder layers
    for i, layer_module in enumerate(self.bert_layers):
      # feed the encoding from the last bert_layer to the next
      #print("Encode layer: ", i, " task_id: ", task_id, " hidden_states: ", hidden_states.shape, " attention_mask: ", extended_attention_mask.shape)
      hidden_states = layer_module(hidden_states, extended_attention_mask, task_id=task_id)
      #print("hidden_states after layer: ", hidden_states.shape)

    return hidden_states

  def forward(self, input_ids, attention_mask, task_id):
    """
    input_ids: [batch_size, seq_len], seq_len is the max length of the batch
    attention_mask: same size as input_ids, 1 represents non-padding tokens, 0 represents padding tokens
    """
    # get the embedding for each input token
    embedding_output = self.embed(input_ids=input_ids)

    # feed to a transformer (a stack of BertLayers)
    sequence_output = self.encode(embedding_output, attention_mask=attention_mask, task_id=task_id)

    # get cls token hidden state
    first_tk = sequence_output[:, 0]
    first_tk = self.pooler_dense(first_tk)
    first_tk = self.pooler_af(first_tk)

    return {'last_hidden_state': sequence_output, 'pooler_output': first_tk}

  