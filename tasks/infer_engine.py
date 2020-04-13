from utilities.lang_data_utils import GlyphStrawboss

hi_glyph = GlyphStrawboss("hi")
en_glyph = GlyphStrawboss("en")

##============ RNN Based =======================================================
import torch
from hypotheses.training_98.recurrent_nets_98 import rnn_model
weight_path = "hypotheses/training_98/Training_98_model-348.pth"
# load Model from source_files itself

weights = torch.load( weight_path, map_location=torch.device('cpu'))
model.load_state_dict(weights)
model.eval()

def inferencer(word):
    in_vec = torch.from_numpy(en_glyph.word2xlitvec(word))
    out = model.inference(in_vec)
    result = hi_glyph.xlitvec2word(out.numpy())
    return [result]


if __name__ == "__main__":
    while(1):
        a = input()
        result = inferencer(a)
        print(result)