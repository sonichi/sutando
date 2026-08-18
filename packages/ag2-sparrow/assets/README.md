# ag2-sparrow — brand assets

Charcoal + lime scheme (variant 11). Every file is generated from one shared
path definition, so the bird geometry is identical across the whole set.

## Color tokens

| Token | Hex | Use |
|---|---|---|
| `charcoal` | `#18181B` | background, mark on light surfaces |
| `offwhite` | `#F7F7F5` | mark on dark surfaces |
| `lime` | `#9BEA36` | wing accent only — never the whole bird |
| `lime-soft` *(alt)* | `#A8D45C` | lower-saturation swap for large surfaces |

The eye is a **knockout** (a real hole) in every transparent mark, so it picks
up whatever sits behind it. In the background versions it is filled with the
background color instead.

## What to use where

| File | Use |
|---|---|
| `svg/icon-charcoal.svg` | default app icon, rounded square |
| `svg/icon-light.svg` | app icon on light/white UI |
| `svg/mark-on-dark.svg` | logo on a dark surface, transparent |
| `svg/mark-on-light.svg` | logo on a light surface, transparent |
| `svg/mark-mono-*.svg` | single-color silhouette — print, stamps, ≤16px, stickers |
| `svg/icon-maskable.svg` | Android adaptive / PWA maskable (full bleed, 70% safe area) |
| `lockup/lockup-on-*.svg` | README header, docs, slides |
| `social/og-image.png` | GitHub social preview / OpenGraph (1280×640) |
| `favicon/favicon.ico` | 16+32+48 multi-resolution |
| `favicon/apple-touch-icon.png` | iOS home screen (180×180) |

## Rules

- Keep clear space around the mark equal to the bird's head height.
- Do not recolor the body and wing to the same value — the wing accent is the
  only thing carrying the mark at small sizes.
- Below 24px use `mark-mono-*` or `icon-charcoal`; the wing detail stops
  resolving and the silhouette does the work.
- Do not add strokes, shadows, or gradients. The mark is flat by design.

## Regenerating

All SVGs are hand-written paths — no font or external dependency. To change the
palette, edit the hex values directly in the SVG files; the four tokens above
are the only colors used.
