#This library contains the SMART regularization finetuning 
#and comes from https://github.com/archinetai/smart-pytorch 
#Authors : Haoming Jiang and Pengcheng He and Weizhu Chen and Xiaodong Liu and Jianfeng Gao and Tuo Zhao
from smart_pytorch import SMARTLoss, kl_loss, sym_kl_loss

def smart_regularization(loss_value, smart_loss_weight, embeddings, logits, last_layers):
    """
    This function applies the SMART regularization finetuning to the loss.
    """
    #Define SMART loss
    smart_loss_fn = SMARTLoss(eval_fn = last_layers, loss_fn = kl_loss, loss_last_fn = sym_kl_loss,
                            num_steps = 1,          # Number of optimization steps to find noise (default = 1)
                            step_size = 1e-5,       # Step size to improve noise (default = 1e-3)
                            epsilon = 1e-6,         # Noise norm constraint (default = 1e-6)
                            noise_var = 1e-6        # Initial noise variance (default = 1e-5)
                            )         
    #Compute SMART loss
    loss_value += smart_loss_weight * smart_loss_fn(embeddings, logits)    
    return loss_value
