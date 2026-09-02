# Editorial font bundle

These font files are bundled for deterministic `archiveDossier` rendering:

| file | SHA-256 |
| --- | --- |
| `NotoSans-Regular.ttf` | `89c3c497f618fdaa0b2d1e98fef93582f28c71debd2c4a8cdf41f190ced2909d` |
| `NotoSans-Bold.ttf` | `e83493c945848ecd4a9ad0f6d19164541a0d3e23a9c952304a00a46e00272ac5` |
| `NotoSerif-Regular.ttf` | `9d7583b7dc9e812afd32a14280c5cac3160012efe50c8d08938f4fea266ff67f` |
| `NotoSerif-Bold.ttf` | `0af0ff2be8f84910fb21ec5fe1b6b7395e3073250502a334baf6ca2f860c88fe` |

The files are unmodified copies from Ubuntu's `fonts-noto-core` package. Noto is distributed under
the SIL Open Font License 1.1; the complete package copyright and license notice is preserved in
`LICENSE-NOTO.txt`.

`backend.editorial.renderer.editorial_font_manifest()` verifies the files at runtime and rolls
their combined digest into the Editorial workflow version. Do not replace a font without updating
this table and verifying portrait and landscape layout tests.
