import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import { parseMessage } from "./attachments";
import { conversationMessage, type ConversationExport } from "./conversationMarkdown";
import { imageStatusLabel, validRunImages } from "./runImages";

function imageSource(value: string): string | undefined {
  if (/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value)) return value;
  try {
    const url = new URL(value, window.location.href);
    if (["https:", "http:"].includes(url.protocol)) return url.href;
  } catch { /* Unusable references retain their caption in the export. */ }
  return undefined;
}

function PdfImage({ src, alt, unavailable }: { src?: string; alt: string; unavailable?: string }) {
  const url = src && imageSource(src);
  return <span className="pdf-image">
    {url && !unavailable ? <>
      <img src={url} alt={alt} loading="eager" referrerPolicy="no-referrer" />
      <span className="pdf-image-fallback" hidden>图片加载失败：{alt}</span>
    </> : <span className="pdf-image-unavailable">{unavailable || "图片地址不可用"}：{alt}</span>}
  </span>;
}

function UserContent({ content }: { content: string }) {
  const { text, attachments } = parseMessage(content);
  return <div className="user-message" role="group" aria-label="用户输入">
    {text && <div className="pdf-user-text">{text}</div>}
    {attachments.map(file => file.kind === "image"
      ? <figure className="pdf-attachment" key={file.id}>
        <PdfImage src={api.imageUrl(file.id)} alt={file.name} />
        <figcaption>{file.name} · {file.width} × {file.height}</figcaption>
      </figure>
      : <section className="pdf-attachment" key={file.id}>
        <header>{file.name}</header><pre>{file.content || "（空文件）"}</pre>
      </section>)}
  </div>;
}

function AnswerContent({ content }: { content: string }) {
  return <article className="assistant-message" aria-label="MiniCode 回复">
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
      urlTransform={(url, key) => key === "src" ? imageSource(url) || "" : /^(?:https?:\/\/|mailto:)/i.test(url) ? url : ""}
      components={{
        a: ({ children, href }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span className="pdf-file-reference">{children}</span>,
        img: ({ src, alt }) => <PdfImage src={typeof src === "string" ? src : undefined} alt={alt || "聊天图片"} />,
      }}>{content}</ReactMarkdown>
  </article>;
}

/** Render the complete saved conversation, independent of viewport and collapsed cards. */
export function ConversationAppearance({ conversation }: { conversation: ConversationExport }) {
  const seenImages = new Set<string>();
  const turns = conversation.turns.map(turn => {
    const items = turn.displayMessages.flatMap((message, index) => {
      const visible = conversationMessage(message);
      if (visible) return [visible.role === "user" ? <UserContent key={index} content={visible.content} />
        : <AnswerContent key={index} content={visible.content} />];
      if (message.role !== "tool_image") return [];
      const images = validRunImages(message.images).filter(image => {
        const key = `${image.sessionId}:${image.id}`;
        if (seenImages.has(key)) return false;
        seenImages.add(key);
        return true;
      });
      return images.length ? [<div className="pdf-generated-images" key={index} aria-label="执行截图与图片">
        {images.map(image => <figure key={image.id}>
          <PdfImage src={api.runImageUrl(image.sessionId, image.id)} alt={image.caption || image.name}
            unavailable={image.availability === "cleared" ? imageStatusLabel(image.availability) : undefined} />
          <figcaption>{image.caption || image.name}</figcaption>
        </figure>)}
      </div>] : [];
    });
    return items.length ? <section className="pdf-chat-turn" key={turn.turnId} data-turn-id={turn.turnId}>{items}</section> : null;
  }).filter(Boolean);
  if (!turns.length) throw new Error("当前聊天还没有可导出的消息。");
  return <>
    <header className="pdf-chat-heading"><span>MiniCode · 聊天记录</span><h1>{conversation.title || "MiniCode 聊天"}</h1></header>
    {turns}
  </>;
}
