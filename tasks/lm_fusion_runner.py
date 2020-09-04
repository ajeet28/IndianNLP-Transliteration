''' RNN Seq2Seq (Encoder-Decoder) training setup
'''

import torch
from torch.utils.data import DataLoader
import numpy as np
import os
import sys
from tqdm import tqdm
import utilities.running_utils as rutl
from utilities.lang_data_utils import XlitData, MonoCharLMData, GlyphStrawboss, merge_xlit_jsons
from utilities.logging_utils import LOG2CSV
from algorithms.lm_fused_rnn import Encoder, Decoder, Seq2SeqLMFusion, LMDecoder


##===== Init Setup =============================================================
MODE = rutl.RunMode.train
INST_NAME = "Training_Test"

##------------------------------------------------------------------------------
device = 'cuda' if torch.cuda.is_available() else 'cpu'

LOG_PATH = "hypotheses/"+INST_NAME+"/"
WGT_PREFIX = LOG_PATH+"weights/"+INST_NAME
if not os.path.exists(LOG_PATH+"weights"): os.makedirs(LOG_PATH+"weights")

##===== Running Configuration =================================================
src_glyph = GlyphStrawboss("en")
tgt_glyph = GlyphStrawboss("hi")

num_epochs = 1000
batch_size = 3
acc_grad = 1
learning_rate = 1e-3
teacher_forcing, teach_force_till, teach_decay_pereph = 1, 10, 0

## Merge JSON into a single file
# train_file = merge_xlit_jsons(["data/hindi/HiEn_train1.json",
#                                 "data/hindi/HiEn_train2.json" ],
#                                 save_prefix= LOG_PATH)

train_dataset = XlitData( src_glyph_obj = src_glyph, tgt_glyph_obj = tgt_glyph,
                        json_file='data/konkani/GomEn_ann1_train.json', file_map = "LangEn",
                        padding=True)

## Charset training for predicting next char
# train_dataset = MonoCharLMData(glyph_obj = tgt_glyph,
#                     data_file = "data/konkani/gom_all_words_sorted.json",
#                     input_type = 'plain',
#                     shift_left_tgt = True,
#                     padding = True, )

train_dataloader = DataLoader(train_dataset, batch_size=batch_size,
                                shuffle=True, num_workers=0)

val_dataset = XlitData( src_glyph_obj = src_glyph, tgt_glyph_obj = tgt_glyph,
                        json_file='data/konkani/GomEn_ann1_valid.json', file_map = "LangEn",
                        padding=True)
# val_dataset = train_dataset

val_dataloader = DataLoader(val_dataset, batch_size=batch_size,
                                shuffle=True, num_workers=0)

# for i in range(len(train_dataset)):
#     print(train_dataset.__getitem__(i))


##===== Model Configuration =================================================
for_deep_fusion = True
##------------- Basenet -----------------------------------------------------

input_dim = src_glyph.size()
output_dim = tgt_glyph.size()
enc_emb_dim = 300
dec_emb_dim = 300
enc_hidden_dim = 512
dec_hidden_dim = 512
rnn_type = "lstm"
enc2dec_hid = True
attention = True
enc_layers = 1
dec_layers = 2
m_dropout = 0
enc_bidirect = True
enc_outstate_dim = enc_hidden_dim * (2 if enc_bidirect else 1)

enc = Encoder(  input_dim= input_dim, embed_dim = enc_emb_dim,
                hidden_dim= enc_hidden_dim,
                rnn_type = rnn_type, layers= enc_layers,
                dropout= m_dropout, device = device,
                bidirectional= enc_bidirect)
dec = Decoder(  output_dim= output_dim, embed_dim = dec_emb_dim,
                hidden_dim= dec_hidden_dim,
                rnn_type = rnn_type, layers= dec_layers,
                dropout= m_dropout,
                use_attention = attention,
                enc_outstate_dim= enc_outstate_dim,
                for_deep_fusion= for_deep_fusion, # <<< Check
                device = device,)


# model = Seq2SeqLMFusion(enc, dec, pass_enc2dec_hid=enc2dec_hid,
#                 device=device)
# model = model.to(device)
# model_func = model.basenet_forward

### ----- Load Embeds -----

# hi_emb_vecs = np.load("data/embeds/fasttext/hi_99_char_300d_fasttext.npy")
# model.decoder.embedding.weight.data.copy_(torch.from_numpy(hi_emb_vecs))

# en_emb_vecs = np.load("data/embeds/fasttext/en_char_300d_fasttext.npy")
# model.encoder.embedding.weight.data.copy_(torch.from_numpy(en_emb_vecs))

# pretrain_wgt_path = None
# model = rutl.load_pretrained(model,pretrain_wgt_path) #if path empty returns unmodified


##--------------- Langauge Model -----------------------------------------------

tgt_glyph = GlyphStrawboss("hi")
output_dim = tgt_glyph.size()
dec_emb_dim = 300
dec_hidden_dim = 512
rnn_type = "lstm"
dec_layers = 2
m_dropout = 0

lm_dec = LMDecoder(  output_dim= output_dim, embed_dim = dec_emb_dim,
                hidden_dim= dec_hidden_dim,
                rnn_type = rnn_type, layers= dec_layers,
                for_deep_fusion = for_deep_fusion, # <<< Check
                dropout= m_dropout,
                device = device,)

# model = lm_dec
# model_func = lm_dec.forward
# model = model.to(device)

##------------ Fused Net -------------------------------------------------------

model = Seq2SeqLMFusion(
                encoder = enc,
                decoder = dec,
                pass_enc2dec_hid=enc2dec_hid,
                lm_decoder = lm_dec,
                for_deep_fusion = for_deep_fusion, # <<< Check
                dropout = 0, device = device)

model = model.to(device)

model.fusion_initial_weight_loader(basewgt_path ='hypotheses/Training_gom_121/Training_gom_121_base/weights/Training_gom_121_base_model.pth' ,
                    lmwgt_path='hypotheses/Training_gom_121/Training_gom_121_lm/weights/Training_gom_121_lm_lmnet_model.pth' ,)
## freeze LM for training
model.lm_decoder = rutl.freeze_params(model.lm_decoder)
model_func = model.deep_fuse_forward

##------ Model Details ---------------------------------------------------------
rutl.count_train_param(model)
print(model)


##====== Optimizer Zone ===================================================================


criterion = torch.nn.CrossEntropyLoss()
    # weight = torch.from_numpy(train_dataset.tgt_class_weights).to(device)  )

def loss_estimator(pred, truth):
    """ Only consider non-zero inputs in the loss; mask needed
    pred: batch
    """
    mask = truth.ge(1).type(torch.FloatTensor).to(device)
    loss_ = criterion(pred, truth) * mask
    return torch.mean(loss_)

optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate,
                             weight_decay=0)

# scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.1)

#===============================================================================

if __name__ =="__main__":

    best_loss = float("inf")
    best_accuracy = 0
    for epoch in range(num_epochs):

        #-------- Training -------------------
        model.train()
        acc_loss = 0
        running_loss = []
        if epoch >= teach_force_till: teacher_forcing = 0
        else: teacher_forcing = max(0, teacher_forcing - teach_decay_pereph)

        for ith, (src, tgt, src_sz) in enumerate(train_dataloader):

            src = src.to(device)
            tgt = tgt.to(device)

            #--- forward ------
            output = model_func(src = src, tgt = tgt, src_sz =src_sz,
                            teacher_forcing_ratio = teacher_forcing)
            loss = loss_estimator(output, tgt) / acc_grad
            acc_loss += loss

            #--- backward ------
            loss.backward()
            if ( (ith+1) % acc_grad == 0):
                optimizer.step()
                optimizer.zero_grad()

                print('epoch[{}/{}], MiniBatch-{} loss:{:.4f}'
                    .format(epoch+1, num_epochs, (ith+1)//acc_grad, acc_loss.data))
                running_loss.append(acc_loss.item())
                acc_loss=0
                # break # <<<

        LOG2CSV(running_loss, LOG_PATH+"trainLoss.csv")

        #--------- Validate ---------------------
        model.eval()
        val_loss = 0
        val_accuracy = 0
        for jth, (v_src, v_tgt, v_src_sz) in enumerate(tqdm(val_dataloader)):
            v_src = v_src.to(device)
            v_tgt = v_tgt.to(device)
            with torch.no_grad():
                v_output = model_func(src = v_src, tgt = v_tgt, src_sz = v_src_sz)
                val_loss += loss_estimator(v_output, v_tgt)

                val_accuracy += rutl.accuracy_score(v_output, v_tgt, tgt_glyph)
            # break # <<<
        val_loss = val_loss / len(val_dataloader)
        val_accuracy = val_accuracy / len(val_dataloader)

        print('epoch[{}/{}], [-----TEST------] loss:{:.4f}  Accur:{:.4f}'
              .format(epoch+1, num_epochs, val_loss.data, val_accuracy.data))
        LOG2CSV([val_loss.item(), val_accuracy.item()],
                    LOG_PATH+"valLoss.csv")

        #-------- save Checkpoint -------------------
        if val_accuracy > best_accuracy:
        # if val_loss < best_loss:
            print("***saving best optimal state [Loss:{} Accur:{}] ***".format(val_loss.data,val_accuracy.data) )
            best_loss = val_loss
            best_accuracy = val_accuracy
            torch.save(model.state_dict(), WGT_PREFIX+"_model-{}.pth".format(epoch+1))
            LOG2CSV([epoch+1, val_loss.item(), val_accuracy.item()],
                    LOG_PATH+"bestCheckpoint.csv")

        # LR step
        # scheduler.step()