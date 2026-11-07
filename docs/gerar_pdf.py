"""Gera docs/yksim-manual.pdf a partir do README.md (manual de instalação e uso).

    python3 docs/gerar_pdf.py            # precisa de: pip install markdown weasyprint
"""
import sys
from pathlib import Path

import markdown
from markdown.extensions.toc import slugify_unicode
from weasyprint import HTML

root = Path(__file__).resolve().parent.parent
out = str(root / "docs" / "yksim-manual.pdf") if len(sys.argv) < 2 else sys.argv[1]
md = (root / "README.md").read_text()
# <details> do GitHub: no papel, mostra o conteúdo aberto
md = md.replace("<details>\n<summary>", '<div class="details" markdown="1">\n<p class="summary">').replace(
    "</summary>", "</p>\n").replace("</details>", "</div>")
import re
# fenced_code não funciona dentro de listas: vira bloco recuado (8 espaços = código dentro do item)
md = re.sub(r"^    ```[a-z]*\n(.*?)^    ```$",
            lambda m: "\n".join("        " + l[4:] for l in m.group(1).rstrip("\n").split("\n")),
            md, flags=re.M | re.S)
body = markdown.markdown(md, extensions=["tables", "fenced_code", "toc", "md_in_html", "sane_lists"],
                         extension_configs={"toc": {"slugify": slugify_unicode}}, output_format="html5")

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm;
  @bottom-center { content: "yksim — simulador de YubiKey 5 NFC · " counter(page) "/" counter(pages);
                   font: 8pt 'DejaVu Sans'; color: #888; } }
@page :first { @bottom-center { content: none; } }
.cover { break-after: page; padding-top: 40mm; text-align: center; }
.kicker { color: #8a6a1f; font-weight: bold; letter-spacing: 1pt; text-transform: uppercase; font-size: 10pt; }
.ctitle { font-size: 30pt; border: 0; margin: 6pt 0 10pt; }
.csub { color: #555; font-size: 11pt; margin: 0 20mm 14mm; }
.cover img { width: 150mm; }
.cdate { color: #888; margin-top: 12mm; }
html { font: 10pt/1.5 'DejaVu Sans', sans-serif; color: #1f2328; }
h1 { font-size: 21pt; margin: 0 0 6pt; color: #111; border-bottom: 2px solid #d4a93c; padding-bottom: 6pt; }
h2 { font-size: 14.5pt; margin: 20pt 0 6pt; color: #1a3d7c; border-bottom: 1px solid #d0d7de; padding-bottom: 3pt;
     break-after: avoid; }
h3, p.summary { font-size: 11pt; font-weight: bold; break-after: avoid; }
a { color: #1a5fb4; text-decoration: none; }
code { font: 8.6pt 'DejaVu Sans Mono', monospace; background: #f3f4f6; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4pt; padding: 6pt 8pt;
      white-space: pre-wrap; word-break: break-all; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.2pt; }
table { border-collapse: collapse; width: 100%; font-size: 8.6pt; margin: 6pt 0; break-inside: auto; }
th, td { border: 1px solid #d0d7de; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #eef1f4; }
tr { break-inside: avoid; }
td code { font-size: 7.8pt; word-break: break-all; }
img { max-width: 100%; display: block; margin: 8pt auto; border: 1px solid #d0d7de; border-radius: 4pt;
      break-inside: avoid; }
p:has(> img) { break-inside: avoid; }
blockquote { margin: 6pt 0; padding: 4pt 10pt; border-left: 3px solid #d4a93c; background: #fff8e6; color: #5a4310; }
hr { display: none; }
.details { border: 1px dashed #c9ced6; border-radius: 4pt; padding: 4pt 8pt; }
ul, ol { padding-left: 16pt; }
"""
import datetime
cover = f"""<section class="cover">
<p class="kicker">yksim · YubiKey 5 NFC virtual com PIV</p>
<h1 class="ctitle">Manual de instalação e uso</h1>
<p class="csub">Debian 13 e Ubuntu 24.04 no WSL2 · dependências, instalação passo a passo, primeiro uso,
tutoriais e solução de problemas</p>
<img src="docs/img/janela.png" alt="Janela do simulador">
<p class="cdate">{datetime.date.today():%d/%m/%Y}</p>
</section>"""
html = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>yksim — manual de instalação e uso</title>
<style>{CSS}</style></head><body>{cover}{body}</body></html>"""
HTML(string=html, base_url=str(root) + "/").write_pdf(out)
print("ok", out)
