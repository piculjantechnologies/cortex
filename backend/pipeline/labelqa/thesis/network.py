# Vendored from the label-quality-assurance repository,
# https://github.com/piculjantechnologies/label-quality-assurance
# thesis/neural_network.py at commit 07f31da. AGPL-3.0-only.
#
# CorrNet is copied verbatim; only the imports it needs are kept. "README item
# N" in its comments refers to thesis/README.md of that repository.
"""CorrNet: the network of the Pascal VOC checkpoint that the scoring stage loads (see MODEL.md)."""
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import ResNet18_Weights, resnet18


class CorrNet(nn.Module):
    """Two full ResNet-18 branches with correlation fusion and the per-cell
    label-plane head -- the network this release trains.

    Both branches run to layer4 (stride 32; 7 x 7 for 224-px inputs): the
    image branch is ImageNet-pretrained (fine-tuned by train.py at 0.1 x
    the learning rate, README item 5; its BatchNorm statistics kept at the
    pretrained values, README item 2), the label branch is randomly
    initialised with a num_classes-channel first convolution. The
    classifier reads both branches globally average-pooled and three
    agreement terms between the aligned stride-32 maps (README item 2):
    their element-wise product averaged over the grid, and, on
    L2-normalised features, the product averaged over the cells the label
    draws on (masked) and its per-channel maximum -- 5 x 512 features
    through fully connected layers 512, 256, 128 and 2. With raster_head
    (README item 4), the per-cell head reads the image branch's own layer2
    (stride 8) and layer3 (stride 16) next to the label planes and adds the
    claimed cells' smooth minimum, mean and log(1 + count) to the good
    logit through box_mix, initialised at zero.
    """

    corr = True     # image-grounded fusion (read by interactive_demo.py)

    def __init__(self, num_classes=20, pretrained=True, raster_head=False):
        super().__init__()
        self.raster_head = raster_head
        img = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1
                       if pretrained else None)
        self.model_1 = nn.Sequential(*list(img.children())[:-2])
        lbl = resnet18()
        lbl.conv1 = nn.Conv2d(num_classes, 64, kernel_size=7, stride=2,
                              padding=3, bias=False)
        self.model_2 = nn.Sequential(*list(lbl.children())[:-2])
        if raster_head:
            def block(cin, cout, dil):
                return [nn.Conv2d(cin, cout, 3, padding=dil, dilation=dil),
                        nn.GroupNorm(16, cout), nn.ReLU()]
            self.rh_body = nn.Sequential(
                *block(128 + 256 + num_classes + 64, 256, 1),
                *block(256, 256, 2), *block(256, 256, 4),
                *block(256, 128, 8))
            self.rh_support = nn.Conv2d(128, num_classes, 1)
            self.rh_cls = nn.Conv2d(128, num_classes + 1, 1)
            self.box_mix = nn.Linear(3, 1)
            nn.init.zeros_(self.box_mix.weight)
            nn.init.zeros_(self.box_mix.bias)
        self.fc = nn.Sequential(
            nn.Flatten(),
            nn.Linear(512 * 5, 512), nn.BatchNorm1d(512), nn.ReLU(),
            nn.Dropout(),
            nn.Linear(512, 256), nn.BatchNorm1d(256), nn.ReLU(),
            nn.Dropout(),
            nn.Linear(256, 128), nn.BatchNorm1d(128), nn.ReLU(),
            nn.Dropout(),
            nn.Linear(128, 2))

    def train(self, mode=True):
        super().train(mode)
        if mode:
            # the pretrained image branch keeps its BatchNorm statistics
            # (README item 2); its weights are fine-tuned (item 5)
            for m in self.model_1.modules():
                if isinstance(m, nn.modules.batchnorm._BatchNorm):
                    m.eval()
        return self

    @staticmethod
    def _box_masked(prod, label):
        """Average the agreement over the cells the label draws on."""
        stride = label.shape[-1] // prod.shape[-1]
        mask = (F.max_pool2d(label.amax(1, keepdim=True), stride)
                > 0).to(prod.dtype)
        return (prod * mask).sum(dim=(2, 3)) / \
            mask.sum(dim=(2, 3)).clamp(min=1.0)

    def forward(self, img, label, aux=False):
        """aux=True (a raster_head model) also returns the head's per-cell
        support (B, num_classes, g, g) and class logits
        (B, num_classes + 1, g, g), which its training losses read."""
        # resnet18's children: [:5] ends after layer1 (stride 4), then
        # layer2 (stride 8), layer3 (stride 16), layer4 (stride 32)
        i8 = self.model_1[5](self.model_1[:5](img))
        i16 = self.model_1[6](i8)
        f_img = self.model_1[7](i16)
        f_lbl = self.model_2(label)
        cos = F.normalize(f_img, dim=1) * F.normalize(f_lbl, dim=1)
        parts = [f_img.mean(dim=(2, 3)), f_lbl.mean(dim=(2, 3)),
                 (f_img * f_lbl).mean(dim=(2, 3)),
                 self._box_masked(cos, label), cos.amax(dim=(2, 3))]
        logits = self.fc(torch.cat(parts, 1))
        if not self.raster_head:
            return logits
        return self._raster(logits, label, i8, i16, aux)

    def _raster(self, logits, label, i8, i16, aux=False):
        """The label-plane head: per-cell class support and class, and the
        claimed cells' support mixed into the good logit."""
        k = label.shape[-1] // i8.shape[-1]
        r_cls = F.max_pool2d(label, k)
        r_geo = F.pixel_unshuffle(label.amax(1, keepdim=True), k)
        h = self.rh_body(torch.cat(
            [i8, F.interpolate(i16, size=i8.shape[-2:], mode="bilinear",
                               align_corners=False),
             r_cls.to(i8.dtype), r_geo.to(i8.dtype)], 1))
        sup = self.rh_support(h).float()
        claimed = r_cls > 0
        cnt = claimed.flatten(1).sum(1).float()
        has = cnt > 0
        # smooth minimum -logsumexp(-v) over the claimed cells; unclaimed
        # cells enter at a finite -1e4 (not -inf) so a label with nothing
        # drawn backpropagates zeros rather than NaN
        smin = -torch.logsumexp(
            torch.where(claimed, -sup, torch.full_like(sup, -1e4))
            .flatten(1), 1)
        smin = torch.where(has, smin, torch.zeros_like(smin))
        mean = torch.where(claimed, sup, torch.zeros_like(sup)) \
            .flatten(1).sum(1) / cnt.clamp(min=1)
        delta = self.box_mix(torch.stack(
            [smin, mean, torch.log1p(cnt)], 1)).squeeze(1)
        out = logits + torch.stack([torch.zeros_like(delta), delta],
                                   1).to(logits.dtype)
        return (out, sup, self.rh_cls(h)) if aux else out
