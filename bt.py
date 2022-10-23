import torch
from timm.models.vision_transformer import Attention, Block

class BlockWrap(torch.nn.Module):

    def __init__(self, block: Block, dim, args):
        super().__init__()
        self.block = block
        self.attn_bt = Attention(dim, num_heads=6, qkv_bias=0., attn_drop=0.5, proj_drop=args.drop)
        from timm.models.layers import DropPath
        self.drop_path_bt = DropPath(args.drop_path_bt) if args.drop_path_bt > 0. else torch.nn.Identity()
        self.norm3 = torch.nn.LayerNorm(dim, eps=1e-6)

    def forward(self, x):
        x = x + self.block.drop_path(self.block.attn(self.block.norm1(x)))
        x = x + self.block.drop_path(self.block.mlp(self.block.norm2(x)))
        if self.training:
            old_x = x
            old_x = old_x.transpose(0, 1)
            x = x + self.drop_path_bt(self.norm3(self.attn_bt(old_x).transpose(0, 1)))
        # x = self.block(x)
        return x


class BlockBF(torch.nn.Module):

    def __init__(self, block: Block, fp16_type=0):
        super().__init__()
        self.block = block
        self.fp16_type = fp16_type

    def forward(self, x):
        from torch.cuda.amp import autocast
        if self.fp16_type == 2:

            with autocast(enabled=False):
                old_x = self.block.norm1(x)
                x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.attn(old_x.float()), 1, 0)), 1, 0)
                old_x = self.block.norm2(x)
            x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.mlp(old_x), 1, 0)), 1, 0)
        elif self.fp16_type == 0:
            x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.attn(self.block.norm1(x)), 1, 0)), 1, 0)
            x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.mlp(self.block.norm2(x)), 1, 0)), 1, 0)
        else:
            old_x = self.block.norm1(x)
            with autocast(enabled=False):
                x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.attn(old_x.float()), 1, 0)), 1, 0)
            x = x + torch.transpose(self.block.drop_path(torch.transpose(self.block.mlp(self.block.norm2(x)), 1, 0)), 1, 0)


        return x


class BlockWrap32(torch.nn.Module):

    def __init__(self, block: Block, fp16_type: int):
        super().__init__()
        self.block = block
        self.fp16_type = fp16_type

    def forward(self, x):
        from torch.cuda.amp import autocast
        if self.fp16_type == 2:

            with autocast(enabled=False):
                old_x = self.block.norm1(x)
                x = x + self.block.drop_path(self.block.attn(old_x.float()))
                old_x = self.block.norm2(x)
            x = x + self.block.drop_path(self.block.mlp(old_x))
        else:
            old_x = self.block.norm1(x)
            with autocast(enabled=False):
                x = x + self.block.drop_path(self.block.attn(old_x.float()))
            x = x + self.block.drop_path(self.block.mlp(self.block.norm2(x)))
        return x

class BlockWrapDebug(torch.nn.Module):

    def __init__(self, block: Block):
        super().__init__()
        self.block = block

    def forward(self, x):
        tmp_x = self.block.drop_path(self.block.attn(self.block.norm1(x)))
        for n,p in self.block.named_parameters():
            if n.__contains__('attn.qkv.weight'):
                print(n, p.detach().cpu().numpy(), p.grad)
        x = x + tmp_x
        tmp_x = self.block.drop_path(self.block.mlp(self.block.norm2(x)))

        x = x + tmp_x
        return x

class AttentionOnly(torch.nn.Module):

    def __init__(self, block: Block, drop_path = 0. , add_global=70):
        super().__init__()
        self.block = block
        from timm.models.layers import DropPath
        self.add_global=add_global
        self.drop_path = DropPath(drop_path) if drop_path > 0. else torch.nn.Identity()
        self.block.mlp = None
        self.block.norm2 = None

    def forward(self, x):
        if self.add_global==70:
            x = x + torch.transpose(self.drop_path(torch.transpose(self.block.attn(self.block.norm1(x)), 0, 1)), 0, 1)
        else:
            x = x + self.drop_path(self.block.attn(self.block.norm1(x)))
        # x = x + self.block.drop_path(self.block.mlp(self.block.norm2(x)))
        return x

class MLPDecorder(torch.nn.Module):

    def __init__(self, dim, mlp_dim, skip_mlp=False):
        super().__init__()
        self.skip_mlp = skip_mlp
        self.mlp = torch.nn.Sequential(torch.nn.Linear(dim, mlp_dim), torch.nn.BatchNorm1d(mlp_dim), torch.nn.ReLU(),
                                       torch.nn.Linear(mlp_dim, dim), torch.nn.BatchNorm1d(dim), )

    def forward(self, x):
        if self.training and not self.skip_mlp :
            bt_x = x[len(x)//2:]
            old_x = x[:len(x)//2]
            bt_x = self.mlp(bt_x)
            x = torch.cat([old_x, bt_x], dim=0)
        return x

class MLPEncoder(torch.nn.Module):

    def __init__(self, d_model, batch_size, dim_feedforward=2048, dropout=0.1, activation="relu"):
        super(MLPEncoder, self).__init__()
        # self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = torch.nn.Linear(batch_size, batch_size)

        self.norm1 = torch.nn.LayerNorm(d_model)
        self.dropout1 = torch.nn.Dropout(dropout)
        import torch.nn.functional as F
        self.activation = F.relu

    def __setstate__(self, state):
        if 'activation' not in state:
            import torch.nn.functional as F
            state['activation'] = F.relu
        super(MLPEncoder, self).__setstate__(state)

    def forward(self, src):
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """
        src2 = torch.transpose(src, 0, 2)
        src2 = self.linear1(src2)
        src2 = torch.transpose(src2, 0, 2)
        src = src + self.dropout1(src2)
        src = self.norm1(src)

        return src


class TransformerEncoderLayerInner(torch.nn.Module):
    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1, activation="relu"):
        super(TransformerEncoderLayerInner, self).__init__()
        from torch.nn import MultiheadAttention
        self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = torch.nn.Linear(d_model, dim_feedforward)
        self.dropout = torch.nn.Dropout(dropout)
        self.linear2 = torch.nn.Linear(dim_feedforward, d_model)

        from torch.nn import LayerNorm
        self.norm1 = LayerNorm(d_model)
        self.norm2 = torch.nn.LayerNorm(d_model)
        from torch.nn import Dropout
        self.dropout1 = Dropout(dropout)
        self.dropout2 = torch.nn.Dropout(dropout)
        import torch.nn.functional as F
        self.activation = F.relu

    def __setstate__(self, state):
        if 'activation' not in state:
            import torch.nn.functional as F
            state['activation'] = F.relu
        super(TransformerEncoderLayerInner, self).__setstate__(state)

    def forward(self, src):
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """
        src2, attn_output_weights = self.self_attn(src, src, src, attn_mask=None,
                              key_padding_mask=None)
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src, attn_output_weights




class TransformerDecorator1(torch.nn.Module):
    def __init__(self, add_global=3, dim=2048, eval_global=0, small_seq=False, args=None, first_layer=False, drop_path=0.1):
        super(TransformerDecorator1, self).__init__()
        self.not_cls_token = False
        self.no_fp16_bt = 0
        self.shuffle_patch=False
        self.cls_token_only=False
        heads = 4
        self.empty_bt = 0
        self.mlp_enc = 0
        self.batch_size = 128
        self.skip_bt = False
        if args is not None:
            self.not_cls_token = args.not_cls_token
            self.cls_token_only = args.cls_token_only
            self.no_fp16_bt = args.no_fp16_bt
            self.mlp_enc = args.mlp_enc
            heads = args.num_heads
            self.shuffle_patch = args.shuffle_patch
            self.empty_bt = args.empty_bt
            self.batch_size = args.batch_size
            self.all_patches = args.all_patches
            self.skip_bt = args.skip_bt
        self.atten_weights = None
        self.encoder_layers = torch.nn.TransformerEncoderLayer(dim, heads, dim, args.bt_atten_drop)
        self.eval_global = eval_global
        self.add_global = add_global
        self.first_layer = first_layer
        # if add_global in [63]:
        #     self.encoder_layers = TransformerEncoderLayerL(dim, heads, dim, args.bt_atten_drop)
        #     print('only attention')
        # elif add_global in [66]:
        #     self.encoder_layers = TransformerEncoderLayerA(dim, heads, dim, args.bt_atten_drop)
        # elif add_global in [55, 70, 76, 71, 59, 67, 68, 69, 72, 73, 74, 77, 75, 81, 82]:
        #     # 76 is for 70 (drop patch with mlp) while 74 is for 70 (drop batch)
        #     # 75 is for 71 (expand batch)
        #     # 69 is attention only with two branches, 73 is two branches with drop patch
        #     from functools import partial
        #     self.encoder_layers = Block(
        #         dim=dim, num_heads=heads, mlp_ratio=4, qkv_bias=True, drop=args.drop,
        #         attn_drop=args.bt_atten_drop, drop_path=drop_path, norm_layer=partial(torch.nn.LayerNorm, eps=1e-6), act_layer=torch.nn.GELU)
        #     if add_global in [75, 71]:
        #         self.encoder_layers = BlockBF(self.encoder_layers)
        #     if add_global in [70, 74, 77, 69]: # atten only
        #         self.encoder_layers = AttentionOnly(self.encoder_layers, drop_path=drop_path, add_global=add_global)
        if self.mlp_enc == 1:
            self.encoder_layers = MLPEncoder(dim, args.batch_size)

        if self.empty_bt:
            self.encoder_layers = torch.nn.Identity()
        self.idx = 0
        self.noise = None
        self.layer_num = 1
        self.small_seq = small_seq
        if self.first_layer:
            self.add_global = 1


    def forward(self, feature):
        if self.training and self.add_global > 0 and not self.skip_bt:
            old_feature = feature
            if self.add_global in [1, 2, 3] and not self.first_layer:
                # split
                old_feature = feature[:len(feature)//2]
                feature = feature[-len(feature)//2:]
            if self.shuffle_patch:
                B, L, C = feature.shape
                idx_orig_list = []
                idx_shuffle_list = []
                for i in range(len(feature)):
                    idx = torch.arange(0, L).type(torch.LongTensor)
                    idx_orig = torch.zeros(L).type(torch.LongTensor)
                    idx_shuffle = torch.cat([torch.randperm(1).type(torch.LongTensor),torch.randperm(L -1 ).type(torch.LongTensor)], 0)

                    idx_orig[idx_shuffle] = idx + i*L
                    idx_shuffle_list.append(idx_shuffle+ i * L)
                    # new_features.append(feature[i][idx_shuffle])
                    idx_orig_list.append(idx_orig)
                idx_shuffle = torch.stack(idx_shuffle_list, 0).reshape(-1).to(feature.device)
                idx_orig_list = torch.stack(idx_orig_list, 0).reshape(-1).to(feature.device)
                feature = feature.view(B*L, C)[idx_shuffle].view(B, L, C)
                pass

            size = feature.shape
            if len(size) == 3: #vit
                if self.all_patches:
                    stride = 4
                    # N L C
                    size = feature.shape
                    feature = torch.transpose(feature, 1, 0) # L N C
                    feature = torch.reshape(feature, (stride * size[1], size[0] // stride, size[2]))
                if isinstance(self.encoder_layers, Block) or \
                        isinstance(self.encoder_layers, Attention) or \
                        isinstance(self.encoder_layers, BlockBF) or isinstance(self.encoder_layers, AttentionOnly):
                    feature = feature.transpose(0, 1)
                elif self.not_cls_token: # do not use in the paper
                    feature1 = self.encoder_layers(feature[:, 1:, :])
                    feature = torch.cat([feature[:, :1, :], feature1], dim=1)
                elif self.cls_token_only: # do not use in the paper
                    feature1 = self.encoder_layers(feature[:, :1, :])
                    feature = torch.cat([feature1, feature[:, 1:, :]], dim=1)
                elif self.no_fp16_bt:
                    from torch.cuda.amp import autocast
                    with autocast(enabled=False):
                        feature = self.encoder_layers(feature.float())
                else:
                    feature = self.encoder_layers(feature)
                if isinstance(self.encoder_layers, Block) or \
                        isinstance(self.encoder_layers, Attention) or \
                        isinstance(self.encoder_layers, BlockBF) or isinstance(self.encoder_layers, AttentionOnly):
                    feature = feature.transpose(0, 1)
                if self.all_patches:
                    # recover
                    feature = torch.reshape(feature, (size[1], size[0], size[2]))
                    feature = torch.transpose(feature, 1, 0)
            else:
                feature = feature.view(feature.size(0), feature.size(1), -1)
                feature = torch.transpose(feature, 2, 1)
                feature = self.encoder_layers(feature)
                feature = torch.transpose(feature, 2, 1)
                feature = feature.view(size)
            if self.shuffle_patch:
                feature = feature.view(B*L, C)[idx_orig_list].view(B, L, C)
            if self.add_global not in [1]:
                feature = torch.cat([old_feature, feature], dim=0)
                # print(self.noise, self.idx)
            # print(feature.norm())
            return feature
        elif self.add_global and self.eval_global:
            if isinstance(self.encoder_layers, Block) or \
                    isinstance(self.encoder_layers, Attention):
                feature = feature.transpose(0, 1)
            if self.add_global and self.eval_global == 2:
                size = feature.shape
                old_feature = feature
                feature = torch.reshape(feature, (size[0] * size[1], 1, size[2]))
                feature = self.encoder_layers(feature)
                feature = torch.reshape(feature, (size[0], size[1], size[2])) # Acc@1 80.152 Acc@5 95.160 loss 0.849
                # feature = torch.cat([old_feature, feature], dim=0)
            elif self.add_global and self.eval_global == 1:
                feature = self.encoder_layers(feature)
            elif self.add_global and self.eval_global == 3:
                old_feature = feature
                feature = self.encoder_layers(feature)
                # feature = old_feature + feature # 79.628
                feature = torch.cat([old_feature, feature], dim=0)
            if isinstance(self.encoder_layers, Block) or \
                    isinstance(self.encoder_layers, Attention):
                feature = feature.transpose(0, 1)
        return feature


class TransformerEncoderLayerL(torch.nn.Module):
    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1, activation="relu"):
        super(TransformerEncoderLayerL, self).__init__()
        # self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        # self.linear1 = torch.nn.Linear(d_model, dim_feedforward)
        # self.dropout = torch.nn.Dropout(dropout)
        # self.linear2 = torch.nn.Linear(dim_feedforward, d_model)

        # self.norm1 = LayerNorm(d_model)
        self.norm2 = torch.nn.LayerNorm(d_model)
        # self.dropout1 = Dropout(dropout)
        # self.dropout2 = torch.nn.Dropout(dropout)
        import torch.nn.functional as F
        self.activation = F.relu

    def __setstate__(self, state):
        if 'activation' not in state:
            import torch.nn.functional as F
            state['activation'] = F.relu
        super(TransformerEncoderLayerL, self).__setstate__(state)

    def forward(self, src):
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """
        # src2 = self.self_attn(src, src, src, attn_mask=None,
        #                       key_padding_mask=None)[0]
        # src = src + self.dropout1(src2)
        # src = self.norm1(src)
        # src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        # src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src


class TransformerEncoderLayerA(torch.nn.Module):
    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1, activation="relu"):
        super(TransformerEncoderLayerA, self).__init__()
        from torch.nn import MultiheadAttention
        self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        # self.linear1 = torch.nn.Linear(d_model, dim_feedforward)
        # self.dropout = torch.nn.Dropout(dropout)
        # self.linear2 = torch.nn.Linear(dim_feedforward, d_model)

        from torch.nn import LayerNorm
        self.norm1 = LayerNorm(d_model)
        # self.norm2 = torch.nn.LayerNorm(d_model)
        from torch.nn import Dropout
        self.dropout1 = Dropout(dropout)
        # self.dropout2 = torch.nn.Dropout(dropout)
        import torch.nn.functional as F
        self.activation = F.relu

    def __setstate__(self, state):
        if 'activation' not in state:
            import torch.nn.functional as F
            state['activation'] = F.relu
        super(TransformerEncoderLayerA, self).__setstate__(state)

    def forward(self, src):
        r"""Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """
        src2 = self.self_attn(src, src, src, attn_mask=None,
                              key_padding_mask=None)[0]
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        # src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        # src = src + self.dropout2(src2)
        # src = self.norm2(src)
        return src

