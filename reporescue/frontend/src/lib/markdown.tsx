import { useState, type ReactNode } from "react";

/**
 * Minimal Markdown renderer for AI-authored explanations, summaries, and
 * investigation messages. Supports headings, paragraphs, lists, quotes,
 * tables, inline code/emphasis/links, and fenced code blocks with
 * language labels, copy buttons, and lightweight syntax tinting.
 *
 * Safety: the parser builds React elements from plain strings, so raw
 * HTML in model output is rendered as inert text, never interpreted.
 */

export type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "code"; lang: string; code: string }
  | { kind: "table"; head: string[]; rows: string[][] }
  | { kind: "quote"; text: string };

const FENCE_RE = /^```\s*([A-Za-z0-9_+-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const UL_RE = /^\s*[-*+]\s+(.*)$/;
const OL_RE = /^\s*\d+[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const TABLE_SEP_RE = /^\s*\|?[\s:|-]+\|\s*$/;

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  const pushPara = (text: string) => {
    const clean = text.trim();
    if (clean) blocks.push({ kind: "para", text: clean });
  };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const fence = line.match(FENCE_RE);
    if (fence) {
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE_RE.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1; // consume closing fence (or EOF)
      blocks.push({ kind: "code", lang: fence[1] || "text", code: code.join("\n") });
      continue;
    }
    const heading = line.match(HEADING_RE);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length,
        text: heading[2].trim(),
      });
      i += 1;
      continue;
    }
    const quote = line.match(QUOTE_RE);
    if (quote) {
      const quoted: string[] = [quote[1]];
      i += 1;
      while (i < lines.length) {
        const next = lines[i].match(QUOTE_RE);
        if (!next) break;
        quoted.push(next[1]);
        i += 1;
      }
      pushPara(quoted.join(" "));
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "para") {
        blocks[blocks.length - 1] = { kind: "quote", text: last.text };
      }
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push({ kind: "table", head, rows });
      continue;
    }
    const listMatch = line.match(UL_RE) ?? line.match(OL_RE);
    if (listMatch) {
      const ordered = OL_RE.test(line);
      const items: string[] = [];
      while (i < lines.length) {
        const item = lines[i].match(UL_RE) ?? lines[i].match(OL_RE);
        if (!item) break;
        items.push(item[1].trim());
        i += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    const para: string[] = [line.trim()];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE_RE.test(lines[i]) &&
      !HEADING_RE.test(lines[i]) &&
      !QUOTE_RE.test(lines[i]) &&
      !(lines[i].match(UL_RE) ?? lines[i].match(OL_RE))
    ) {
      if (lines[i].includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) break;
      para.push(lines[i].trim());
      i += 1;
    }
    pushPara(para.join(" "));
  }
  return blocks;
}

/** Inline `code`, **bold**, *italic*, and [text](http/https url). */
function Inline({ text }: { text: string }) {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\((?:https?:\/\/[^)\s]+)\))/g;
  let last = 0;
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(text.slice(last, start));
    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push(<code key={key++}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      nodes.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("*")) {
      nodes.push(<em key={key++}>{token.slice(1, -1)}</em>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
      if (link) {
        nodes.push(
          <a key={key++} href={link[2]} target="_blank" rel="noreferrer">
            {link[1]}
          </a>
        );
      } else {
        nodes.push(token);
      }
    }
    last = start + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return <>{nodes}</>;
}

const KEYWORDS = new Set(
  "def return if elif else for while import from as class with try except finally raise pass lambda None True False null true false await async function const let var new typeof instanceof switch case break continue do in of delete void await yield export default extends super this static get new console log print include using namespace std int str bool void public private protected struct enum fn mut impl match use crate mod pub where loop return if else while for in break continue true false nil".split(
    " "
  )
);

/** Lightweight per-line tinting: comments, strings, numbers, keywords. */
function highlightLine(line: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Split off a trailing comment (# or //), ignoring # inside strings is
  // deliberately approximate: this is tinting, not a real tokenizer.
  const commentAt = (() => {
    const hash = line.search(/(^|\s)#/);
    const slash = line.search(/(^|\s)\/\//);
    if (hash === -1) return slash;
    if (slash === -1) return hash;
    return Math.min(hash, slash);
  })();
  let code = line;
  let comment = "";
  if (commentAt !== -1) {
    code = line.slice(0, commentAt);
    comment = line.slice(commentAt);
  }
  const pattern = /("[^"\n]*"|'[^'\n]*'|\b[A-Za-z_][\w$]*|\b\d[\d._]*\b)/g;
  let last = 0;
  let key = 0;
  for (const match of code.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(code.slice(last, start));
    const token = match[0];
    if (token.startsWith('"') || token.startsWith("'")) {
      nodes.push(
        <span key={`${keyPrefix}-${key++}`} className="tok-str">
          {token}
        </span>
      );
    } else if (/^\d/.test(token)) {
      nodes.push(
        <span key={`${keyPrefix}-${key++}`} className="tok-num">
          {token}
        </span>
      );
    } else if (KEYWORDS.has(token)) {
      nodes.push(
        <span key={`${keyPrefix}-${key++}`} className="tok-kw">
          {token}
        </span>
      );
    } else {
      nodes.push(token);
    }
    last = start + token.length;
  }
  if (last < code.length) nodes.push(code.slice(last));
  if (comment) {
    nodes.push(
      <span key={`${keyPrefix}-c`} className="tok-com">
        {comment}
      </span>
    );
  }
  return nodes;
}

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="codeblock">
      <div className="codeblock-bar">
        <span>{lang}</span>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>
        <code>
          {code.split("\n").map((line, index) => (
            <span key={index} className="code-line">
              {highlightLine(line || " ", `l${index}`)}
              {"\n"}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
}

export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  const blocks = parseMarkdown(text);
  return (
    <div className={`md ${className}`.trim()}>
      {blocks.map((block, index) => {
        switch (block.kind) {
          case "heading": {
            const Tag = `h${Math.min(block.level + 1, 6)}` as "h2" | "h3" | "h4" | "h5" | "h6";
            return (
              <Tag key={index}>
                <Inline text={block.text} />
              </Tag>
            );
          }
          case "para":
            return (
              <p key={index}>
                <Inline text={block.text} />
              </p>
            );
          case "quote":
            return (
              <blockquote key={index}>
                <Inline text={block.text} />
              </blockquote>
            );
          case "list":
            return block.ordered ? (
              <ol key={index}>
                {block.items.map((item, itemIndex) => (
                  <li key={itemIndex}>
                    <Inline text={item} />
                  </li>
                ))}
              </ol>
            ) : (
              <ul key={index}>
                {block.items.map((item, itemIndex) => (
                  <li key={itemIndex}>
                    <Inline text={item} />
                  </li>
                ))}
              </ul>
            );
          case "table":
            return (
              <table key={index}>
                <thead>
                  <tr>
                    {block.head.map((cell, cellIndex) => (
                      <th key={cellIndex}>
                        <Inline text={cell} />
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {block.rows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex}>
                          <Inline text={cell} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          case "code":
            return <CodeBlock key={index} lang={block.lang} code={block.code} />;
        }
      })}
    </div>
  );
}
