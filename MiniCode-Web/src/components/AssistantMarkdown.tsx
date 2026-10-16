import { FileCode2, FileImage, FileJson, FileText } from "lucide-react";
import { createContext, useContext } from "react";
import type { Root } from "mdast";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Plugin } from "unified";

import { fileReferenceKind, fileReferenceTitle, parseFileReference } from "../lib/fileReferences";
import type { FileReference } from "../lib/fileReferences";

export const FileNavigationContext = createContext<{
  workspace?: string;
  onOpenFile?: (reference: FileReference) => void;
}>({});

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  position?: Root["position"];
  children?: MarkdownNode[];
}

const remarkAutolinkBoundaries: Plugin<[], Root> = function () {
  const processor = this;
  return (tree, file) => {
    let markdown = String(file.value);
    let normalizedTree = tree;
    while (true) {
      const replacements: { start: number; end: number; value: string }[] = [];
      function visit(parent: MarkdownNode) {
        if (["link", "linkReference", "code", "html"].includes(parent.type)) return;
        for (const child of parent.children || []) {
          const label = child.children?.[0];
          const start = child.position?.start.offset;
          const end = child.position?.end.offset;
          // GFM bare links share their start with the label; explicit links start at `[` or `<`.
          if (child.type === "link" && child.children?.length === 1 && label?.type === "text"
            && start !== undefined && end !== undefined && start === label.position?.start.offset) {
            const raw = markdown.slice(start, end);
            const boundary = raw.search(/[，。；：！？、（）【】「」『』“”‘’《》]/);
            if (/^(?:https?:\/\/|www\.)/i.test(raw) && boundary > 0) {
              // Parse the URL in isolation to keep closing Markdown markers outside it.
              const paragraph = (processor.parse(raw.slice(0, boundary)) as Root).children[0];
              const link = paragraph?.type === "paragraph" ? paragraph.children[0] : undefined;
              const length = link?.position?.end.offset;
              if (link?.type === "link" && length && link.children.length === 1 && link.children[0].type === "text") {
                const text = link.children[0].value.replace(/&/g, "&amp;").replace(/[\\[\]`*_~]/g, "\\$&");
                const url = link.url.replace(/&/g, "&amp;").replace(/[\\<>]/g, "\\$&");
                replacements.push({ start, end: start + length, value: `[${text}](<${url}>)` });
              }
            }
          } else visit(child);
        }
      }
      visit(normalizedTree);
      if (!replacements.length) {
        file.value = markdown;
        return normalizedTree;
      }
      for (const replacement of replacements.sort((a, b) => b.start - a.start)) {
        markdown = markdown.slice(0, replacement.start) + replacement.value + markdown.slice(replacement.end);
      }
      // Reparse the bounded links so surrounding emphasis and any newly exposed URLs resolve normally.
      normalizedTree = processor.parse(markdown) as Root;
    }
  };
};

function remarkFileReferences() {
  return (tree: Root) => {
    function visit(parent: MarkdownNode) {
      if (["link", "linkReference", "code", "html"].includes(parent.type)) return;
      parent.children?.forEach((child, index) => {
        if (child.type === "inlineCode" && child.value && parseFileReference(child.value, true)) {
          parent.children![index] = {
            type: "link", url: child.value,
            children: [{ type: "text", value: child.value }],
          };
        } else visit(child);
      });
    }
    visit(tree);
  };
}

const fileIcons = { code: FileCode2, json: FileJson, text: FileText, image: FileImage };

export function AssistantMarkdown({ content }: { content: string }) {
  const navigation = useContext(FileNavigationContext);
  return <ReactMarkdown remarkPlugins={[remarkGfm, remarkAutolinkBoundaries, remarkFileReferences]}
    urlTransform={(url, key) => key === "href" && parseFileReference(url) ? url : defaultUrlTransform(url)}
    components={{
      a: ({ children, href, node: _node, ...props }) => {
        const reference = href && parseFileReference(href);
        if (!reference) return <a {...props} href={href} target="_blank" rel="noreferrer">{children}</a>;
        const Icon = fileIcons[fileReferenceKind(reference.path)];
        const title = fileReferenceTitle(reference);
        const suffixInLabel = typeof children === "string" && /(?:\(line \d+\)|[:#]L?\d+(?::\d+|C\d+)?)$/.test(children);
        return <button type="button" className="file-reference" title={title}
          disabled={!navigation.workspace || !navigation.onOpenFile}
          aria-label={`打开文件 ${title}`} onClick={() => navigation.onOpenFile?.(reference)}>
          <Icon size={15} aria-hidden="true" />
          <span>{children}</span>
          {reference.line && !suffixInLabel && <span className="file-reference-location">(line {reference.line})</span>}
        </button>;
      },
    }}>{content}</ReactMarkdown>;
}
