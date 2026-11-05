import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Markdown, parseMarkdown } from "./markdown";

describe("markdown parser", () => {
  it("parses headings, paragraphs, lists, and code blocks", () => {
    const blocks = parseMarkdown(
      "# Overview\n\nSome text.\n\n- one\n- two\n\n```bash\necho hi\n```\n"
    );
    expect(blocks.map((block) => block.kind)).toEqual([
      "heading",
      "para",
      "list",
      "code",
    ]);
    expect(blocks[0]).toMatchObject({ level: 1, text: "Overview" });
    expect(blocks[3]).toMatchObject({ lang: "bash", code: "echo hi" });
  });

  it("parses tables and quotes", () => {
    const blocks = parseMarkdown(
      "| A | B |\n|---|---|\n| 1 | 2 |\n\n> quoted\n"
    );
    expect(blocks[0]).toMatchObject({
      kind: "table",
      head: ["A", "B"],
      rows: [["1", "2"]],
    });
    expect(blocks[1]).toMatchObject({ kind: "quote", text: "quoted" });
  });

  it("renders raw HTML as inert text", () => {
    render(<Markdown text="<script>alert(1)</script>" />);
    expect(screen.getByText("<script>alert(1)</script>")).toBeDefined();
    expect(document.querySelector("script")).toBeNull();
  });

  it("renders inline code, bold, and links", () => {
    const { container } = render(
      <Markdown text="Use `pip install x` for **setup** and see [docs](https://example.com/x)." />
    );
    expect(container.querySelector("code")?.textContent).toBe("pip install x");
    expect(container.querySelector("strong")?.textContent).toBe("setup");
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com/x");
    expect(link?.getAttribute("rel")).toBe("noreferrer");
  });

  it("renders copyable code blocks with tinted tokens", () => {
    const { container } = render(
      <Markdown text={'```python\ndef add(a, b):\n    return a + 1  # sum\n```'} />
    );
    expect(screen.getByText("Copy")).toBeDefined();
    expect(screen.getByText("python")).toBeDefined();
    expect(container.querySelector(".tok-kw")?.textContent).toBe("def");
    expect(container.querySelector(".tok-num")?.textContent).toBe("1");
    expect(container.querySelector(".tok-com")?.textContent).toContain("# sum");
  });

  it("copy button does not crash without clipboard access", () => {
    render(<Markdown text={"```bash\necho hi\n```"} />);
    fireEvent.click(screen.getByText("Copy"));
    expect(screen.getByText("Copy")).toBeDefined();
  });
});
