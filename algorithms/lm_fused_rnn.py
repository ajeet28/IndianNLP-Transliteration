"""
Network designed with reference to: Using Monolingual Corpora in Neural Machine Translation
https://arxiv.org/pdf/1503.03535.pdf
"""

import torch
import torch.nn as nn
import random
import sys

class Encoder(nn.Module):
    '''
    Simple RNN based encoder network
    '''
    def __init__(self, input_dim, embed_dim, hidden_dim ,
                       rnn_type = 'gru', layers = 1,
                       bidirectional =False,
                       dropout = 0, device = "cpu"):
        super(Encoder, self).__init__()

        self.input_dim = input_dim #src_vocab_sz
        self.enc_embed_dim = embed_dim
        self.enc_hidden_dim = hidden_dim
        self.enc_rnn_type = rnn_type
        self.enc_layers = layers
        self.enc_directions = 2 if bidirectional else 1
        self.device = device

        self.embedding = nn.Embedding(self.input_dim, self.enc_embed_dim)

        if self.enc_rnn_type == "gru":
            self.enc_rnn = nn.GRU(input_size= self.enc_embed_dim,
                          hidden_size= self.enc_hidden_dim,
                          num_layers= self.enc_layers,
                          bidirectional= bidirectional)
        elif self.enc_rnn_type == "lstm":
            self.enc_rnn = nn.LSTM(input_size= self.enc_embed_dim,
                          hidden_size= self.enc_hidden_dim,
                          num_layers= self.enc_layers,
                          bidirectional= bidirectional)
        else:
            raise Exception("unknown RNN type mentioned")

    def forward(self, x, x_sz, hidden = None):
        '''
        x_sz: (batch_size, 1) -  Unpadded sequence lengths used for pack_pad
        '''
        batch_sz = x.shape[0]
        # x: batch_size, max_length, enc_embed_dim
        x = self.embedding(x)

        ## pack the padded data
        # x: max_length, batch_size, enc_embed_dim -> for pack_pad
        x = x.permute(1,0,2)
        x = nn.utils.rnn.pack_padded_sequence(x, x_sz, enforce_sorted=False) # unpad

        # output: packed_size, batch_size, enc_embed_dim
        # hidden: n_layer**num_directions, batch_size, hidden_dim | if LSTM (h_n, c_n)
        output, hidden = self.enc_rnn(x) # gru returns hidden state of all timesteps as well as hidden state at last timestep

        ## pad the sequence to the max length in the batch
        # output: max_length, batch_size, enc_emb_dim*directions)
        output, _ = nn.utils.rnn.pad_packed_sequence(output)

        # output: batch_size, max_length, hidden_dim
        output = output.permute(1,0,2)

        return output, hidden


class Decoder(nn.Module):
    '''
    Used as decoder stage
    '''
    def __init__(self, output_dim, embed_dim, hidden_dim,
                       rnn_type = 'gru', layers = 1,
                       use_attention = True,
                       enc_outstate_dim = None, # enc_directions * enc_hidden_dim
                       for_deep_fusion = False,
                       dropout = 0, device = "cpu"):
        super(Decoder, self).__init__()

        self.output_dim = output_dim #tgt_vocab_sz
        self.dec_hidden_dim = hidden_dim
        self.dec_embed_dim = embed_dim
        self.dec_rnn_type = rnn_type
        self.dec_layers = layers
        self.use_attention = use_attention
        self.device = device
        if self.use_attention:
            self.enc_outstate_dim = enc_outstate_dim if enc_outstate_dim else hidden_dim
        else:
            self.enc_outstate_dim = 0


        self.embedding = nn.Embedding(self.output_dim, self.dec_embed_dim)

        if self.dec_rnn_type == 'gru':
            self.dec_rnn = nn.GRU(input_size= self.dec_embed_dim + self.enc_outstate_dim, # to concat attention_output
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        elif self.dec_rnn_type == "lstm":
            self.dec_rnn = nn.LSTM(input_size= self.dec_embed_dim + self.enc_outstate_dim, # to concat attention_output
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        else:
            raise Exception("unknown RNN type mentioned")

        if not for_deep_fusion: #only get_hidden works
            self.fc = nn.Sequential(
                nn.Linear(self.dec_hidden_dim, self.dec_embed_dim), nn.LeakyReLU(),
                # nn.Linear(self.dec_embed_dim, self.dec_embed_dim), nn.LeakyReLU(), # removing to reduce size
                nn.Linear(self.dec_embed_dim, self.output_dim),
                )

        ##----- Attention ----------
        if self.use_attention:
            self.W1 = nn.Linear( self.enc_outstate_dim, self.dec_hidden_dim)
            self.W2 = nn.Linear( self.dec_hidden_dim, self.dec_hidden_dim)
            self.V = nn.Linear( self.dec_hidden_dim, 1)

    def attention(self, x, hidden, enc_output):
        '''
        x: (batch_size, 1, dec_embed_dim) -> after Embedding
        enc_output: batch_size, max_length, enc_hidden_dim *num_directions
        hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        '''

        ## perform addition to calculate the score

        # hidden_with_time_axis: batch_size, 1, hidden_dim
        ## hidden_with_time_axis = hidden.permute(1, 0, 2) ## replaced with below 2lines
        hidden_with_time_axis = torch.sum(hidden, axis=0)

        hidden_with_time_axis = hidden_with_time_axis.unsqueeze(1)

        # score: batch_size, max_length, hidden_dim
        score = torch.tanh(self.W1(enc_output) + self.W2(hidden_with_time_axis))

        # attention_weights: batch_size, max_length, 1
        # we get 1 at the last axis because we are applying score to self.V
        attention_weights = torch.softmax(self.V(score), dim=1)

        # context_vector shape after sum == (batch_size, hidden_dim)
        context_vector = attention_weights * enc_output
        context_vector = torch.sum(context_vector, dim=1)
        # context_vector: batch_size, 1, hidden_dim
        context_vector = context_vector.unsqueeze(1)

        # attend_out (batch_size, 1, dec_embed_dim + hidden_size)
        attend_out = torch.cat((context_vector, x), -1)

        return attend_out, attention_weights

    def forward(self, x, hidden, enc_output):
        '''
        x: (batch_size, 1)
        enc_output: batch_size, max_length, dec_embed_dim
        hidden: n_layer, batch_size, hidden_size | lstm: (h_n, c_n)
        '''
        if (hidden is None) and (self.use_attention is False):
            raise Exception( "No use of a decoder with No attention and No Hidden")

        batch_sz = x.shape[0]

        if hidden is None:
            # hidden: n_layers, batch_size, hidden_dim
            hid_for_att = torch.zeros((self.dec_layers, batch_sz,
                                    self.dec_hidden_dim )).to(self.device)
        elif self.dec_rnn_type == 'lstm':
            hid_for_att = hidden[1] #<<<< c_n used; Should have been h_n
        else:
            hid_for_att = hidden

        # x (batch_size, 1, dec_embed_dim) -> after embedding
        x = self.embedding(x)

        aw = 0
        if self.use_attention:
            # x (batch_size, 1, dec_embed_dim + hidden_size) -> after attention
            # aw: (batch_size, max_length, 1)
            x, aw = self.attention( x, hid_for_att, enc_output)


        # passing the concatenated vector to the GRU
        # output: (batch_size, n_layers, hidden_size)
        # hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        output, hidden = self.dec_rnn(x, hidden) if hidden is not None else self.dec_rnn(x)

        # output :shp: (batch_size * 1, hidden_size)
        output =  output.view(-1, output.size(2))

        # output :shp: (batch_size * 1, output_dim)
        output = self.fc(output)

        return output, hidden, aw


    def get_hidden(self, x, hidden, enc_output):
        '''
        Get Hidden for Deep fusion technique

        x: (batch_size, 1)
        enc_output: batch_size, max_length, dec_embed_dim
        hidden: n_layer, batch_size, hidden_size | lstm: (h_n, c_n)
        '''
        batch_sz = x.shape[0]

        if hidden is None:
            # hidden: n_layers, batch_size, hidden_dim
            hid_for_att = torch.zeros((self.dec_layers, batch_sz,
                                    self.dec_hidden_dim )).to(self.device)
        elif self.dec_rnn_type == 'lstm':
            hid_for_att = hidden[0] # h_n <<<check
        else:
            hid_for_att = hidden

        # x (batch_size, 1, dec_embed_dim) -> after embedding
        x = self.embedding(x)

        aw = 0
        if self.use_attention:
            # x (batch_size, 1, dec_embed_dim + hidden_size) -> after attention
            # aw: (batch_size, max_length, 1)
            x, aw = self.attention( x, hid_for_att, enc_output)

        # passing the concatenated vector to the GRU
        # output: (batch_size, n_layers, hidden_size)
        # hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        output, hidden = self.dec_rnn(x, hidden) if hidden is not None else self.dec_rnn(x)

        return hidden


class LMDecoder(nn.Module):
    '''
    Language model
    Used to train to predict next letter in a sequence
    '''
    def __init__(self, output_dim, embed_dim, hidden_dim,
                       rnn_type = 'gru', layers = 1,
                       for_deep_fusion = False,
                       dropout = 0, device = "cpu"):
        super(LMDecoder, self).__init__()

        self.output_dim = output_dim #tgt_vocab_sz
        self.dec_hidden_dim = hidden_dim
        self.dec_embed_dim = embed_dim
        self.dec_rnn_type = rnn_type
        self.dec_layers = layers
        self.device = device

        self.embedding = nn.Embedding(self.output_dim, self.dec_embed_dim)

        if self.dec_rnn_type == 'gru':
            self.dec_rnn = nn.GRU(input_size= self.dec_embed_dim,
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        elif self.dec_rnn_type == "lstm":
            self.dec_rnn = nn.LSTM(input_size= self.dec_embed_dim,
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        else:
            raise Exception("unknown RNN type mentioned")

        if not for_deep_fusion: # only get_hidden works
            self.fc = nn.Sequential(
                nn.Linear(self.dec_hidden_dim, self.dec_embed_dim), nn.LeakyReLU(),
                # nn.Linear(self.dec_embed_dim, self.dec_embed_dim), nn.LeakyReLU(), # removing to reduce size
                nn.Linear(self.dec_embed_dim, self.output_dim),
                )

    def decoding(self, x, hidden):
        '''
        Decoder routine ( same as Decoder )

        x: (batch_size, 1)
        enc_output: batch_size, max_length, dec_embed_dim
        hidden: n_layer, batch_size, hidden_size | lstm: (h_n, c_n)
        '''
        batch_sz = x.shape[0]

        # x (batch_size, 1, dec_embed_dim) -> after embedding
        x = self.embedding(x)

        # passing the concatenated vector to the GRU
        # output: (batch_size, n_layers, hidden_size)
        # hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        output, hidden = self.dec_rnn(x, hidden) if hidden is not None else self.dec_rnn(x)

        # output :shp: (batch_size * 1, hidden_size)
        output =  output.view(-1, output.size(2))

        # output :shp: (batch_size * 1, output_dim)
        output = self.fc(output)

        return output, hidden

    def forward(self, src, tgt, src_sz, teacher_forcing_ratio = 0):
        ''' Training alone; Not for inference
        '''
        batch_size = src.shape[0]

        # pred_vecs: (batch_size, output_dim, sequence_sz) -> shape required for CELoss
        pred_vecs = torch.zeros(batch_size, self.output_dim, src.size(1)).to(self.device)

        # dec_input: (batch_size, 1)
        dec_input = src[:,0].unsqueeze(1) # initialize to start token
        dec_hidden = None
        pred_vecs[:,1,0] = 1 # Initialize to start tokens all batches
        for t in range(1, src.size(1)):
            # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            # dec_input: (batch_size, 1)
            dec_output, dec_hidden = self.decoding( dec_input,
                                               dec_hidden,)
            pred_vecs[:,:,t] = dec_output

            # Teacher Forcing
            if random.random() < teacher_forcing_ratio:
                dec_input = src[:, t].unsqueeze(1)
            else:
                # prediction: batch_size
                prediction = torch.argmax(dec_output, dim=1)
                dec_input = prediction.unsqueeze(1)

        return pred_vecs #(batch_size, output_dim, sequence_sz)

    def get_hidden(self, x, hidden):
        '''
        Hidden for Deep fusion
        Detaches the backprop flow from tensors

        x: (batch_size, 1)
        enc_output: batch_size, max_length, dec_embed_dim
        hidden: n_layer, batch_size, hidden_size | lstm: (h_n, c_n)
        '''
        batch_sz = 1

        # x (batch_size, 1, dec_embed_dim) -> after embedding
        x = self.embedding(x)

        # passing the concatenated vector to the GRU
        # output: (batch_size, n_layers, hidden_size)
        # hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        output, hidden = self.dec_rnn(x, hidden) if hidden is not None else self.dec_rnn(x)

        return hidden


class Seq2SeqLMFusion(nn.Module):
    '''
    Used to construct seq2seq architecture with encoder decoder LM objects
    '''
    def __init__(self, encoder, decoder,
                pass_enc2dec_hid=False,
                lm_decoder = None,
                for_deep_fusion = False,
                dropout = 0, device = "cpu"):
        super(Seq2SeqLMFusion, self).__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        self.lm_decoder = lm_decoder
        self.pass_enc2dec_hid = pass_enc2dec_hid
        self.for_deep_fusion = for_deep_fusion
        self.dec_hidden_dim = self.decoder.dec_hidden_dim



        if self.pass_enc2dec_hid:
            assert decoder.dec_hidden_dim == encoder.enc_hidden_dim, "Hidden Dimension of encoder and decoder must be same, or unset `pass_enc2dec_hid`"
        if decoder.use_attention:
            assert decoder.enc_outstate_dim == encoder.enc_directions*encoder.enc_hidden_dim,"Set `enc_out_dim` correctly in decoder"
        assert self.pass_enc2dec_hid or decoder.use_attention, "No use of a decoder with No attention and No Hidden from Encoder"

        if for_deep_fusion:
            self.lm_hidden_dim = self.lm_decoder.dec_hidden_dim
            self.lm_ctrl = nn.Sequential(
                nn.Linear(self.lm_hidden_dim, self.lm_hidden_dim),
                nn.Sigmoid() )
            self.dffc = nn.Sequential(
                nn.Linear(self.dec_hidden_dim + self.lm_hidden_dim, self.dec_hidden_dim),
                nn.LeakyReLU(),
                nn.Linear(self.dec_hidden_dim, self.decoder.output_dim),
                )

    def fusion_initial_weight_loader(self, basewgt_path, lmwgt_path, ):
        ''' For loading the basenet and LM weights into the object intially
        ! Key names are usage specific !
        '''

        basewgt_dict = torch.load(basewgt_path, map_location=torch.device(self.device))

        dec_dict = {}
        for k in basewgt_dict.keys():
            if 'decoder.' in k:
                dec_dict[k.replace("decoder.", "")] = basewgt_dict[k]

        enc_dict = {}
        for k in basewgt_dict.keys():
            if 'encoder.' in k:
                enc_dict[k.replace("encoder.", "")] = basewgt_dict[k]

        lmwgt_dict = torch.load(lmwgt_path, map_location=torch.device(self.device))

        ## Remove Obsolete weights
        if self.for_deep_fusion:
            for r in ["fc.0.weight", "fc.0.bias", "fc.2.weight", "fc.2.bias"]:
                dec_dict.pop(r)
                lmwgt_dict.pop(r)

        self.decoder.load_state_dict(dec_dict)
        self.encoder.load_state_dict(enc_dict)
        self.lm_decoder.load_state_dict(lmwgt_dict)

    def basenet_forward(self, src, tgt, src_sz, teacher_forcing_ratio = 0):
        '''
        Basic Seq2seq Encoder decoder Training

        src: (batch_size, sequence_len.padded)
        tgt: (batch_size, sequence_len.padded)
        src_sz: [batch_size, 1] -  Unpadded sequence lengths
        '''
        batch_size = tgt.shape[0]

        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)
        enc_output, enc_hidden = self.encoder(src, src_sz)

        if self.pass_enc2dec_hid:
            dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            dec_hidden = None

        # pred_vecs: (batch_size, output_dim, sequence_sz) -> shape required for CELoss
        pred_vecs = torch.zeros(batch_size, self.decoder.output_dim, tgt.size(1)).to(self.device)

        # dec_input: (batch_size, 1)
        dec_input = tgt[:,0].unsqueeze(1) # initialize to start token
        pred_vecs[:,1,0] = 1 # Initialize to start tokens all batches
        for t in range(1, tgt.size(1)):
            # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            # dec_output: batch_size, output_dim
            # dec_input: (batch_size, 1)
            dec_output, dec_hidden, _ = self.decoder( dec_input,
                                               dec_hidden,
                                               enc_output,  )
            pred_vecs[:,:,t] = dec_output

            # Teacher Forcing
            if random.random() < teacher_forcing_ratio:
                dec_input = tgt[:, t].unsqueeze(1)
            else:
                # prediction: batch_size
                prediction = torch.argmax(dec_output, dim=1)
                dec_input = prediction.unsqueeze(1)

        return pred_vecs #(batch_size, output_dim, sequence_sz)


    def basenet_inference(self, src, beam_width=3, max_tgt_sz=50, heuristics = False):
        '''
        Basic Seq2seq Encoder decoder inferencing
        Active beam search based decoding

        src: (sequence_len)
        '''
        def _avg_score(p_tup):
            ''' Used for Sorting
            TODO: Dividing by length of sequence power alpha as hyperparam
            '''
            return p_tup[0]

        batch_size = 1
        start_tok = src[0]
        end_tok = src[-1]
        src_sz = torch.tensor([len(src)])
        src_ = src.unsqueeze(0)

        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)
        enc_output, enc_hidden = self.encoder(src_, src_sz)

        if self.pass_enc2dec_hid:
        # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            init_dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            init_dec_hidden = None

        # top_pred[][0] = Σ-log_softmax
        # top_pred[][1] = sequence torch.tensor shape: (1)
        # top_pred[][2] = dec_hidden
        top_pred_list = [ (0, start_tok.unsqueeze(0) , init_dec_hidden) ]

        for t in range(max_tgt_sz):
            cur_pred_list = []

            for p_tup in top_pred_list:
                if p_tup[1][-1] == end_tok:
                    cur_pred_list.append(p_tup)
                    continue

                # dec_hidden: dec_layers, 1, hidden_dim
                # dec_output: 1, output_dim
                dec_output, dec_hidden, _ = self.decoder( x = p_tup[1][-1].view(1,1), #dec_input: (1,1)
                                                    hidden = p_tup[2],
                                                    enc_output = enc_output, )

                ## π{prob} = Σ{log(prob)} -> to prevent diminishing
                # dec_output: (1, output_dim)
                dec_output = nn.functional.log_softmax(dec_output, dim=1)
                # pred_topk.values & pred_topk.indices: (1, beam_width)
                pred_topk = torch.topk(dec_output, k=beam_width, dim=1)

                for i in range(beam_width):
                    sig_logsmx_ = p_tup[0] + pred_topk.values[0][i]
                    # seq_tensor_ : (seq_len)
                    seq_tensor_ = torch.cat( (p_tup[1], pred_topk.indices[0][i].view(1)) )

                    cur_pred_list.append( (sig_logsmx_, seq_tensor_, dec_hidden) )

            cur_pred_list.sort(key = _avg_score, reverse =True) # Maximized order
            top_pred_list = cur_pred_list[:beam_width]

            # check if end_tok of all topk
            end_flags_ = [1 if t[1][-1] == end_tok else 0 for t in top_pred_list]
            if beam_width == sum( end_flags_ ): break

        pred_tnsr_list = [t[1] for t in top_pred_list ]

        if heuristics:
            return top_pred_list

        return pred_tnsr_list

    def lm_heuristics(self, src, max_tgt_sz=50):
        '''
        Probabilistic value from LM for a given word used as heuristics externally

        src: (sequence_len)
        '''
        batch_size = 1
        start_tok = src[0]
        end_tok = src[-1]
        max_tgt_sz = len(src)
        src_sz = torch.tensor([len(src)])

        log_probs = []
        dec_hidden = None
        # dec_input: (batch_size, 1)
        dec_input = start_tok.view(1,1) # initialize to start token
        for t in range(1, max_tgt_sz):
            # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            # dec_output: batch_size, output_dim
            # dec_input: (batch_size, 1)
            dec_output, dec_hidden = self.lm_decoder.decoding( dec_input,
                                               dec_hidden)
            # dec_output: (1, output_dim)
            dec_output = nn.functional.log_softmax(dec_output, dim=1)
            log_probs.append(dec_output[0][src[t]])

            dec_input = src[t].view(1,1)
            if torch.eq(dec_input, end_tok):
                break

        prob = sum(log_probs) / len(log_probs)
        # float
        return prob


    def shallow_fuse_inference(self, src, beam_width=3, max_tgt_sz=50):
        '''
        Search based decoding based on Softmax of LM and Basenet
        src: (sequence_len)
        '''
        assert self.lm_decoder is not None, "Fusion cannot work without LM model"
        def _avg_score(p_tup):
            ''' Used for Sorting
            TODO: Dividing by length of sequence power alpha as hyperparam
            '''
            return p_tup[0]

        batch_size = 1
        start_tok = src[0]
        end_tok = src[-1]
        src_sz = torch.tensor([len(src)])
        src_ = src.unsqueeze(0)

        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)
        enc_output, enc_hidden = self.encoder(src_, src_sz)

        if self.pass_enc2dec_hid:
        # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            init_dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            init_dec_hidden = None

        # top_pred[][0] = Σ-log_softmax
        # top_pred[][1] = sequence torch.tensor shape: (1)
        # top_pred[][2] = dec_hidden
        # top_pred[][3] = lm_hidden
        top_pred_list = [ (0, start_tok.unsqueeze(0) , init_dec_hidden, None) ]
        for t in range(max_tgt_sz):
            cur_pred_list = []

            for p_tup in top_pred_list:
                if p_tup[1][-1] == end_tok:
                    cur_pred_list.append(p_tup)
                    continue

                # dec_hidden: dec_layers, 1, hidden_dim
                # dec_output: 1, output_dim
                dec_output, dec_hidden, _ = self.decoder( x = p_tup[1][-1].view(1,1), #dec_input: (1,1)
                                                    hidden = p_tup[2],
                                                    enc_output = enc_output, )

                lm_output, lm_hidden = self.lm_decoder.decoding(x = p_tup[1][-1].view(1,1),
                                                                hidden = p_tup[3])

                ## π{prob} = Σ{log(prob)} -> to prevent diminishing
                # dec_output: (1, output_dim)
                dec_output = nn.functional.log_softmax(dec_output, dim=1)
                lm_output = nn.functional.log_softmax(lm_output, dim=1)

                shl_output = dec_output + lm_output

                # pred_topk.values & pred_topk.indices: (1, beam_width)
                pred_topk = torch.topk(shl_output, k=beam_width, dim=1)

                for i in range(beam_width):
                    sig_logsmx_ = p_tup[0] + pred_topk.values[0][i]
                    # seq_tensor_ : (seq_len)
                    seq_tensor_ = torch.cat( (p_tup[1], pred_topk.indices[0][i].view(1)) )

                    cur_pred_list.append( (sig_logsmx_, seq_tensor_, dec_hidden, lm_hidden) )

            cur_pred_list.sort(key = _avg_score, reverse =True) # Maximized order
            top_pred_list = cur_pred_list[:beam_width]

            # check if end_tok of all topk
            end_flags_ = [1 if t[1][-1] == end_tok else 0 for t in top_pred_list]
            if beam_width == sum( end_flags_ ): break

        pred_tnsr_list = [t[1] for t in top_pred_list ]

        return pred_tnsr_list


    def deep_fuse_forward(self, src, tgt, src_sz, teacher_forcing_ratio = 0):
        '''
        Concatenate LM hidden and Basenet hidden and retrain Fina FC to predict character
        Gated to control influence of LM Hidden

        src: (batch_size, sequence_len.padded)
        tgt: (batch_size, sequence_len.padded)
        src_sz: [batch_size, 1] -  Unpadded sequence lengths
        '''
        batch_size = tgt.shape[0]

        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)
        enc_output, enc_hidden = self.encoder(src, src_sz)

        if self.pass_enc2dec_hid:
            dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            dec_hidden = None

        lm_hidden = None

        # pred_vecs: (batch_size, output_dim, sequence_sz) -> shape required for CELoss
        pred_vecs = torch.zeros(batch_size, self.decoder.output_dim, tgt.size(1)).to(self.device)

        # dec_input: (batch_size, 1)
        dec_input = tgt[:,0].unsqueeze(1) # initialize to start token
        pred_vecs[:,1,0] = 1 # Initialize to start tokens all batches
        for t in range(1, tgt.size(1)):
            # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            # dec_output: batch_size, output_dim
            # dec_input: (batch_size, 1)
            dec_hidden = self.decoder.get_hidden( dec_input,
                                                dec_hidden,
                                                enc_output, )
            lm_hidden = self.lm_decoder.get_hidden(dec_input, lm_hidden)

            #TODO: Generalize below segment for GRU as well
            lm_gated_hidden = self.lm_ctrl(lm_hidden[0][-1]) * lm_hidden[0][-1]
            fused_hidden = torch.cat((dec_hidden[0][-1], lm_gated_hidden) ,dim = -1)

            fused_output = self.dffc(fused_hidden)
            pred_vecs[:,:,t] = fused_output

            # Teacher Forcing
            if random.random() < teacher_forcing_ratio:
                dec_input = tgt[:, t].unsqueeze(1)
            else:
                # prediction: batch_size
                prediction = torch.argmax(fused_output, dim=1)
                dec_input = prediction.unsqueeze(1)

        return pred_vecs #(batch_size, output_dim, sequence_sz)



    def deep_fuse_inference(self, src, beam_width=3, max_tgt_sz=50):
        '''
        Inferencing for deepfused model
        '''

        def _avg_score(p_tup):
            ''' Used for Sorting
            TODO: Dividing by length of sequence power alpha as hyperparam
            '''
            return p_tup[0]

        batch_size = 1
        start_tok = src[0]
        end_tok = src[-1]
        src_sz = torch.tensor([len(src)])
        src_ = src.unsqueeze(0)
        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)

        enc_output, enc_hidden = self.encoder(src_, src_sz)

        if self.pass_enc2dec_hid:
        # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            init_dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            init_dec_hidden = None

        # top_pred[][0] = Σ-log_softmax
        # top_pred[][1] = sequence torch.tensor shape: (1)
        # top_pred[][2] = dec_hidden
        # top_pred[][3] = lm_hidden
        top_pred_list = [ (0, start_tok.unsqueeze(0) , init_dec_hidden, None) ]
        for t in range(max_tgt_sz):
            cur_pred_list = []

            for p_tup in top_pred_list:
                if p_tup[1][-1] == end_tok:
                    cur_pred_list.append(p_tup)
                    continue

                # dec_hidden: dec_layers, 1, hidden_dim
                dec_hidden = self.decoder.get_hidden( x = p_tup[1][-1].view(1,1), #dec_input: (1,1)
                                                    hidden = p_tup[2],
                                                    enc_output = enc_output, )

                lm_hidden = self.lm_decoder.get_hidden(x = p_tup[1][-1].view(1,1),
                                                                hidden = p_tup[3])


                #TODO: Generalize below segment for GRU as well
                lm_gated_hidden = self.lm_ctrl(lm_hidden[0][-1]) * lm_hidden[0][-1]
                fused_hidden = torch.cat((dec_hidden[0][-1], lm_gated_hidden) ,dim = -1)
                fused_output = self.dffc(fused_hidden)

                ## π{prob} = Σ{log(prob)} -> to prevent diminishing
                # dec_output: (1, output_dim)
                fused_output = nn.functional.log_softmax(fused_output, dim=1)

                # pred_topk.values & pred_topk.indices: (1, beam_width)
                pred_topk = torch.topk(fused_output, k=beam_width, dim=1)

                for i in range(beam_width):
                    sig_logsmx_ = p_tup[0] + pred_topk.values[0][i]
                    # seq_tensor_ : (seq_len)
                    seq_tensor_ = torch.cat( (p_tup[1], pred_topk.indices[0][i].view(1)) )

                    cur_pred_list.append( (sig_logsmx_, seq_tensor_, dec_hidden, lm_hidden) )

            cur_pred_list.sort(key = _avg_score, reverse =True) # Maximized order
            top_pred_list = cur_pred_list[:beam_width]

            # check if end_tok of all topk
            end_flags_ = [1 if t[1][-1] == end_tok else 0 for t in top_pred_list]
            if beam_width == sum( end_flags_ ): break

        pred_tnsr_list = [t[1] for t in top_pred_list ]

        return pred_tnsr_list


