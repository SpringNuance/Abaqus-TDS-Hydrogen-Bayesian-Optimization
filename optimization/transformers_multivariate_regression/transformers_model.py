from typing import Optional, Any
import math

import torch
from torch import nn, Tensor
from torch.nn import functional as F
from torch.nn.modules import MultiheadAttention, Linear, Dropout, BatchNorm1d,\
                            TransformerEncoderLayer
import numpy as np

def _get_activation_fn(activation):
    if activation == "relu":
        return F.relu
    elif activation == "gelu":
        return F.gelu
    raise ValueError("activation should be relu/gelu, not {}".format(activation))

# From https://github.com/pytorch/examples/blob/master/word_language_model/model.py

class FixedPositionalEncoding(nn.Module):

    """ Inject some information about the relative or absolute position of the tokens
        in the sequence. The positional encodings have the same dimension as
        the embeddings, so that the two can be summed. Here, we use sine and cosine
        functions of different frequencies.

        PosEncder(pos, 2i) = sin(pos/10000^(2i/d_model))
        PosEncoder(pos, 2i+1) = cos(pos/10000^(2i/d_model))
        where pos is the word position and i is the embed idx
    Args:
        d_model: the embed dim (required).
        dropout: the dropout value (default=0.1).
        max_len_source: the max. length of the incoming sequence (default=1024).
    """

    def __init__(self, d_model, dropout=0.1, max_len=1024, scale_factor=1.0):
        super(FixedPositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)  # positional encoding
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = scale_factor * pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)  # this stores the variable in the state_dict (used for non-trainable variables)

    def forward(self, x):
        """Inputs of forward function
        Args:
            x: the sequence fed to the positional encoder model (required).
        Shape:
            x: [sequence length, batch size, d_model]
            output: [sequence length, batch size, d_model]
        """

        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)

class LearnablePositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=1024):
        super(LearnablePositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        # Each position gets its own embedding
        # Since indices are always 0 ... max_len, we don't have to do a look-up
        self.pe = nn.Parameter(torch.empty(max_len, 1, d_model))  # requires_grad automatically set to True
        nn.init.uniform_(self.pe, -0.02, 0.02)

    def forward(self, x):
        r"""Inputs of forward function
        Args:
            x: the sequence fed to the positional encoder model (required).
        Shape:
            x: [sequence length, batch size, embed dim]
            output: [sequence length, batch size, embed dim]
        """

        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)

class TransformerBatchNormEncoderLayer(nn.modules.Module):
    r"""This transformer encoder layer block is made up of self-attn and feedforward network.
    It differs from TransformerEncoderLayer in torch/nn/modules/transformer.py in that it replaces LayerNorm
    with BatchNorm.

    Args:
        d_model: the number of expected features in the input (required).
        nhead: the number of heads in the multiheadattention models (required).
        dim_feedforward: the dimension of the feedforward network model (default=2048).
        dropout: the dropout value (default=0.1).
        activation: the activation function of intermediate layer, relu or gelu (default=relu).
    """

    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1, activation="relu"):
        super(TransformerBatchNormEncoderLayer, self).__init__()
        self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = Linear(d_model, dim_feedforward)
        self.dropout = Dropout(dropout)
        self.linear2 = Linear(dim_feedforward, d_model)

        self.norm1 = BatchNorm1d(d_model, eps=1e-5)  # normalizes each feature across batch samples and time steps
        self.norm2 = BatchNorm1d(d_model, eps=1e-5)
        self.dropout1 = Dropout(dropout)
        self.dropout2 = Dropout(dropout)

        self.activation = _get_activation_fn(activation)

    def __setstate__(self, state):
        if 'activation' not in state:
            state['activation'] = F.relu
        super(TransformerBatchNormEncoderLayer, self).__setstate__(state)

    def forward(self, src: Tensor, src_mask: Optional[Tensor] = None,
            src_key_padding_mask: Optional[Tensor] = None,
            **kwargs) -> Tensor:
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
        Shape:
            see the docs in Transformer class.
        """
        src2 = self.self_attn(src, src, src, attn_mask=src_mask,
                              key_padding_mask=src_key_padding_mask)[0]
        src = src + self.dropout1(src2)  # (seq_len, batch_size, d_model)
        src = src.permute(1, 2, 0)  # (batch_size, d_model, seq_len)
        # src = src.reshape([src.shape[0], -1])  # (batch_size, seq_length * d_model)
        src = self.norm1(src)
        src = src.permute(2, 0, 1)  # restore (seq_len, batch_size, d_model)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)  # (seq_len, batch_size, d_model)
        src = src.permute(1, 2, 0)  # (batch_size, d_model, seq_len)
        src = self.norm2(src)
        src = src.permute(2, 0, 1)  # restore (seq_len, batch_size, d_model)
        return src
    

class TransformerEncoderRegressor(nn.Module):
    """
    Simplest classifier/regressor. 
    Can be either regressor or classifier because the output does not include
    softmax. Concatenates final layer embeddings and uses 0s to 
    ignore padding embeddings in final output layer.
    """

    def __init__(self, feat_dim, max_len_source,
                 d_model, n_heads, num_layers, dim_feedforward, 
                 activation_name, pos_enc_type="fixed", encoder_layer_type="LayerNorm",
                 dropout=0.1, freeze=False):
        super(TransformerEncoderRegressor, self).__init__()

        self.max_len_source = max_len_source
        self.d_model = d_model
        self.n_heads = n_heads

        self.linear_layer = nn.Linear(feat_dim, d_model)
        
        # Chooose between fixed or learnable positional encoding
        if pos_enc_type == "fixed":
            self.pos_enc = FixedPositionalEncoding(d_model, dropout=dropout*(1.0 - freeze), max_len = max_len_source)
        elif pos_enc_type == "learnable":
            self.pos_enc = LearnablePositionalEncoding(d_model, dropout=dropout*(1.0 - freeze), max_len = max_len_source)
        
        if encoder_layer_type == "LayerNorm":
            encoder_layer = TransformerEncoderLayer(d_model=d_model, nhead= self.n_heads, 
                                                    dim_feedforward=dim_feedforward, 
                                                    dropout=dropout*(1.0 - freeze), 
                                                    activation=activation_name,
                                                    batch_first=False
                                                    )
        elif encoder_layer_type == "BatchNorm":
            encoder_layer = TransformerBatchNormEncoderLayer(d_model=d_model, nhead= self.n_heads,
                                                            dim_feedforward=dim_feedforward,
                                                            dropout=dropout*(1.0 - freeze),
                                                            activation=activation_name
                                                            )

        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers)
        
        self.activate = _get_activation_fn(activation_name)  # "gelu" or "relu

        self.dropout1 = nn.Dropout(dropout)

        self.feat_dim = feat_dim
        # self.num_classes = num_classes
        # self.output_layer = self.build_output_module(d_model, max_len_source, num_classes)

    def forward(self, X):
        """
        Args:
            X: (batch_size, seq_length, feat_dim) torch tensor of masked features (input)
            padding_masks: (batch_size, seq_length) boolean tensor, 1 means keep vector at this position, 0 means padding
        Returns:
            output: (batch_size, num_classes)
        """

        # pytorch convention for transformers is [seq_length, batch_size, feat_dim]. padding_masks [batch_size, feat_dim]
        inp = X.permute(1, 0, 2) # [seq_length, batch_size, feat_dim]

        # project input vectors to d_model dimensional space
        inp = self.linear_layer(inp) * np.sqrt(self.d_model)  # [seq_length, batch_size, d_model] 
        
        # add positional encoding
        inp = self.pos_enc(inp)  # [seq_length, batch_size, d_model]
        #print(inp)
        # NOTE: logic for padding masks is reversed to comply with definition in MultiHeadAttention, TransformerEncoderLayer
        # output = self.transformer_encoder(inp, src_key_padding_mask=None)
        output = self.transformer_encoder(inp)  # (seq_length, batch_size, d_model)
        
        output = self.activate(output)  # the output transformer encoder/decoder embeddings don't include non-linearity
        output = output.permute(1, 0, 2)  # (batch_size, seq_length, d_model)
        output = self.dropout1(output) # (batch_size, seq_length, d_model)
                
        # output is now shape (batch_size, seq_length, d_model)
        return output
        
class TransformerDecoderRegressor(nn.Module):
    def __init__(self, label_dim, max_len_target, 
                 d_model, n_heads, num_layers, 
                 dim_feedforward, dropout=0.1):

        super(TransformerDecoderRegressor, self).__init__()
        
        self.d_model = d_model
        self.linear_layer = nn.Linear(label_dim, d_model)
        self.pos_decoder = FixedPositionalEncoding(d_model, dropout=dropout, max_len=max_len_target)
        decoder_layer = nn.TransformerDecoderLayer(d_model=d_model, nhead=n_heads, 
                                                   dim_feedforward=dim_feedforward, 
                                                   dropout=dropout, activation='gelu',
                                                   batch_first=False)
        self.transformer_decoder = nn.TransformerDecoder(decoder_layer, num_layers)
        self.output_linear = nn.Linear(d_model, label_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, memory, Y):
        """
        Args:
            memory: Encoded memory from the Transformer Encoder [batch_size, max_len_source, d_model]
            Y: Target sequence for training, shifted [batch_size, max_len_target, label_dim]
        Returns:
            output: final output of the decoder [batch_size, tgt_seq_length, label_dim]
        """
        # Positional encoding for the target sequence
        Y = Y.permute(1, 0, 2) # [seq_length, batch_size, label_dim]
        memory = memory.permute(1, 0, 2) # [seq_length, batch_size, d_model]
        Y = self.linear_layer(Y)  # [seq_length, batch_size, d_model]
        Y = self.pos_decoder(Y) # [seq_length, batch_size, d_model]
        Y = self.transformer_decoder.forward(Y, memory) # [seq_length, batch_size, d_model]
        output = self.output_linear(Y) # [seq_length, batch_size, label_dim]
        output = self.dropout(output) # [seq_length, batch_size, label_dim]
        output = output.permute(1, 0, 2) # [batch_size, seq_length, label_dim]
        return output


#################
# Only Encoding #
#################


class TransformerSoleEncoder(nn.Module):
    """
    Simplest classifier/regressor. 
    Can be either regressor or classifier because the output does not include
    softmax. Concatenates final layer embeddings and uses 0s to 
    ignore padding embeddings in final output layer.
    """
        
    def __init__(self, feat_dim, label_dim, max_len_source,
                d_model, n_heads, num_layers, dim_feedforward, 
                activation_name, pos_enc_type="fixed", encoder_layer_type="LayerNorm",
                dropout=0.1, freeze=False):
        
        super(TransformerSoleEncoder, self).__init__()

        self.max_len_source = max_len_source
        self.d_model = d_model
        self.n_heads = n_heads
        
        self.linear_layer = nn.Linear(feat_dim, d_model)

        if pos_enc_type == "fixed":
            self.pos_enc = FixedPositionalEncoding(d_model, dropout=dropout*(1.0 - freeze), max_len = max_len_source)
        elif pos_enc_type == "learnable":
            self.pos_enc = LearnablePositionalEncoding(d_model, dropout=dropout*(1.0 - freeze), max_len = max_len_source)

        if encoder_layer_type == "LayerNorm":
            encoder_layer = TransformerEncoderLayer(d_model, self.n_heads, 
                                                    dim_feedforward, dropout*(1.0 - freeze), 
                                                    activation=activation_name)
        elif encoder_layer_type == "BatchNorm":
            encoder_layer = TransformerBatchNormEncoderLayer(d_model=d_model, nhead= self.n_heads,
                                                            dim_feedforward=dim_feedforward,
                                                            dropout=dropout*(1.0 - freeze),
                                                            activation=activation_name
                                                            )

        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers)

        self.activate = _get_activation_fn(activation_name)  # "gelu" or "relu

        self.dropout1 = nn.Dropout(dropout)

        self.feat_dim = feat_dim
        self.label_dim = label_dim
        self.output_layer = nn.Linear(d_model * max_len_source, label_dim)
        

    def forward(self, X):
        """
        Args:
            X: (batch_size, seq_length, feat_dim) torch tensor of masked features (input)
            padding_masks: (batch_size, seq_length) boolean tensor, 1 means keep vector at this position, 0 means padding
        Returns:
            output: (batch_size, num_classes)
        """

        # pytorch convention for transformers is [seq_length, batch_size, feat_dim]. padding_masks [batch_size, feat_dim]
        inp = X.permute(1, 0, 2) # [seq_length, batch_size, feat_dim]

        # project input vectors to d_model dimensional space
        inp = self.linear_layer(inp) * np.sqrt(self.d_model)  # [seq_length, batch_size, d_model] 
        
        # add positional encoding
        inp = self.pos_enc(inp)  # [seq_length, batch_size, d_model]
        #print(inp)
        # NOTE: logic for padding masks is reversed to comply with definition in MultiHeadAttention, TransformerEncoderLayer
        # output = self.transformer_encoder(inp, src_key_padding_mask=None)
        output = self.transformer_encoder(inp)  # (seq_length, batch_size, d_model)
        
        output = self.activate(output)  # the output transformer encoder/decoder embeddings don't include non-linearity
        output = output.permute(1, 0, 2)  # (batch_size, seq_length, d_model)
        output = self.dropout1(output) # (batch_size, seq_length, d_model)

        output = output.reshape(output.shape[0], -1)  # (batch_size, seq_length * d_model)
        
        output = self.output_layer(output)  # (batch_size, label_dim)
        
        # we need to reshape it to (batch_size, label_dim, 1)
        output = output.unsqueeze(-1)

        return output


    # def build_output_module(self, d_model, max_len_source, num_classes):
    #     # This linear layer will pass 
    #     # (batch_size, seq_length, d_model) -> (batch_size, 
    #     output_layer = nn.Linear(d_model * max_len_source, num_classes)
    #     # no softmax (or log softmax), because CrossEntropyLoss does this internally. If probabilities are needed,
    #     # add F.log_softmax and use NLLoss
    #     return output_layer