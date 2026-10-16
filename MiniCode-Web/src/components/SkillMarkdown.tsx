import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import "./FilePreview.css";

export function SkillMarkdown({ content }: { content: string }) {
  const source = content.replace(/^\uFEFF/, "");
  const metadata = /^---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(source);
  // Skill metadata is already shown in the detail heading; render the document body.
  const body = metadata && /^(?:name|description)[ \t]*:/m.test(metadata[1])
    ? source.slice(metadata[0].length) : source;

  return <div className="file-markdown skill-preview-markdown">
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
      a: ({ children, node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      img: ({ node: _node, ...props }) => <img {...props} loading="lazy" decoding="async" />,
    }}>{body}</ReactMarkdown>
  </div>;
}
