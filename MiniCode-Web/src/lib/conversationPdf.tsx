import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ConversationExport } from "./conversationMarkdown";
import { ConversationAppearance } from "./conversationPdfAppearance";
import appearanceStyles from "./conversationPdfAppearance.css?inline";

const printStyles = `
* { box-sizing: border-box; }
html { color-scheme: light; }
body { margin: 0; color: #24272c; background: #eef0f3; font: 15px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif; }
.print-toolbar { position: sticky; top: 0; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 24px; border-bottom: 1px solid #dfe2e6; background: #fff; }
.print-toolbar strong { display: block; font-size: 15px; }
.print-toolbar p { margin: 2px 0 0; color: #666b75; font-size: 13px; }
.print-toolbar button { min-height: 44px; border: 0; border-radius: 8px; padding: 10px 18px; background: #292c33; color: #fff; font: inherit; font-size: 14px; cursor: pointer; }
.print-toolbar button:focus-visible { outline: 3px solid #8298c1; outline-offset: 3px; }
.print-toolbar button:disabled { opacity: 0.6; cursor: wait; }
.print-toolbar-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.print-toolbar [hidden] { display: none; }
#retry-images { background: #eef0f4; color: #292c33; }
#image-status { max-width: 650px; }
.print-document { max-width: 210mm; margin: 24px auto; padding: 18mm; background: #fff; box-shadow: 0 4px 24px #252a3510; overflow-wrap: anywhere; }
h1, h2, h3, h4, h5, h6 { line-height: 1.4; break-after: avoid; }
h1 { margin: 0 0 24px; font-size: 26px; }
h2 { margin: 24px 0 12px; font-size: 18px; }
h3 { font-size: 16px; }
p { margin: 12px 0; orphans: 3; widows: 3; }
a { color: #245a9b; text-decoration: underline; overflow-wrap: anywhere; }
ul, ol { padding-left: 24px; }
li { margin: 4px 0; }
blockquote { margin: 16px 0; padding: 2px 16px; border-left: 3px solid #c6cbd3; color: #505661; }
pre, code { font-family: "Cascadia Code", Consolas, "Microsoft YaHei", monospace; font-size: 12px; }
code { padding: 2px 4px; background: #f3f4f6; border-radius: 3px; overflow-wrap: anywhere; }
pre { margin: 16px 0; padding: 12px 14px; border: 1px solid #dfe2e6; border-radius: 6px; background: #f6f7f9; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; break-inside: auto; box-decoration-break: clone; }
pre code { padding: 0; background: none; border-radius: 0; font-size: inherit; white-space: inherit; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 16px 0; font-size: 13px; }
thead { display: table-header-group; }
th, td { padding: 7px 9px; border: 1px solid #d4d9e0; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { background: #f1f3f6; }
hr { margin: 24px 0; border: 0; border-top: 1px solid #dfe2e6; }
.image-reference { color: #666b75; font-size: 13px; }
@media (max-width: 600px) { .print-toolbar { padding: 12px 16px; } .print-document { margin: 12px 0; padding: 24px 18px; } }
@page { size: A4; margin: 16mm; @bottom-right { content: counter(page) " / " counter(pages); font: 9pt sans-serif; color: #777; } }
@media print {
  html, body { background: #fff; }
  body { font-size: 10.5pt; }
  .print-toolbar { display: none !important; }
  .print-document { max-width: none; margin: 0; padding: 0; border: 0; box-shadow: none; }
  h1 { font-size: 20pt; } h2 { font-size: 13pt; } h3 { font-size: 11pt; }
  pre, code { font-size: 9pt; }
  pre, th { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}
`;

const printScript = `
const save = document.getElementById("save-pdf");
const retry = document.getElementById("retry-images");
const status = document.getElementById("image-status");
const images = Array.from(document.querySelectorAll(".pdf-image img"));
async function prepareImages(reload = false) {
  save.disabled = true;
  retry.hidden = true;
  if (images.length) status.textContent = "正在加载 " + images.length + " 张图片…";
  await Promise.all(images.map(img => new Promise(resolve => {
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      img.onload = img.onerror = null;
      img.hidden = !ok;
      img.nextElementSibling.hidden = ok;
      img.dataset.failed = ok ? "false" : "true";
      resolve();
    };
    const timer = setTimeout(() => finish(false), 15000);
    img.onload = () => img.decode().then(() => finish(true), () => finish(false));
    img.onerror = () => finish(false);
    if (reload && img.dataset.failed === "true") img.src = img.src;
    if (img.complete) img.decode().then(() => finish(true), () => finish(false));
  })));
  await document.fonts.ready;
  const failed = images.filter(img => img.dataset.failed === "true").length;
  const missing = document.querySelectorAll(".pdf-image-unavailable").length;
  status.textContent = failed + missing ? (failed + missing) + " 张图片未能加载，PDF 会保留位置和说明。" + (failed ? "可重试后保存。" : "")
    : images.length ? "图片已就绪 · 已保留聊天样式，文字可选择和搜索。" : "";
  retry.hidden = !failed;
  save.disabled = false;
}
retry.addEventListener("click", () => prepareImages(true));
save.addEventListener("click", async () => {
  await document.fonts.ready;
  window.print();
});
prepareImages();
`;

/** A self-contained preview stays printable after the original chat is closed. */
export function showConversationPdf(preview: Window, content: string | ConversationExport): void {
  const appearance = typeof content !== "string";
  const heading = appearance ? content.title : content.split("\n", 1)[0].replace(/^# /, "").replace(/\\(.)/g, "$1");
  const title = heading
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").slice(0, 100).trim() || "聊天记录";
  const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
  const html = renderToStaticMarkup(<html lang="zh-CN">
    <head>
      <meta charSet="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <meta name="referrer" content="no-referrer" />
      <meta httpEquiv="Content-Security-Policy" content={`default-src 'none'; ${appearance ? "img-src http: https: data:;" : ""} style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; base-uri 'none'; form-action 'none'`} />
      <title>{`MiniCode - ${title}`}</title>
      <style>{printStyles + (appearance ? appearanceStyles : "")}</style>
    </head>
    <body>
      <header className="print-toolbar">
        <div><strong>聊天 PDF 预览 · {appearance ? "原样版" : "简洁版"}</strong>
          <p>点击保存，在打印窗口选择“另存为 PDF”。可关闭浏览器页眉页脚，使用文档页码。</p>
          <p id="image-status" role="status" aria-live="polite">{appearance ? "正在准备图片…" : ""}</p></div>
        <div className="print-toolbar-actions"><button id="retry-images" type="button" hidden>重试图片</button>
          <button id="save-pdf" type="button" disabled>保存为 PDF</button></div>
      </header>
      <main className={`print-document${appearance ? " is-appearance" : ""}`} aria-label="聊天导出内容">
        {appearance ? <ConversationAppearance conversation={content} /> : <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
          a: ({ children, href }) => /^https?:\/\//i.test(href || "") || /^mailto:/i.test(href || "")
            ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
          img: ({ alt }) => <span className="image-reference">[图片：{alt || "图片附件"}]</span>,
        }}>{content}</ReactMarkdown>}
      </main>
      <script nonce={nonce} dangerouslySetInnerHTML={{ __html: printScript }} />
    </body>
  </html>);
  preview.document.open();
  preview.document.write(`<!doctype html>${html}`);
  preview.document.close();
}
