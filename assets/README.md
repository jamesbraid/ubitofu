# Brand assets

Mascot: a tofu block wearing a UniFi access point (with antenna), holding an
OpenTofu gear — "ubitofu".

| File | Use |
|------|-----|
| `ubitofu-mascot.png` | Master logo, transparent background, full resolution |
| `ubitofu-mascot.webp` | Same, WebP for web embedding |
| `ubitofu-mascot-white.png` | Opaque white-background variant |
| `logo.png` | README banner (alias of the master) |
| `icon-{16,32,48,64,128,256,512}.png` | Square, transparent, padded app/avatar icons |
| `favicon.ico` | Multi-resolution favicon (16/32/48) |
| `ubitofu-social.png` | 1280×640 GitHub social-preview card |
| `src/ubitofu-mascot-original.png` | Original artwork (untrimmed, white background) |

All PNG variants are derived from `src/` via ImageMagick: transparent background
by corner-connected flood-fill (preserves the white AP-hat interior), trimmed to
the mascot bounding box, then padded/scaled per target.

The GitHub social card must be uploaded manually under
**Settings → General → Social preview** (`ubitofu-social.png`).
