import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Mark } from "./logo";

describe("Verity mark", () => {
  it("renders an accessible scalable vector logo", () => {
    const html = renderToStaticMarkup(<Mark />);

    expect(html).toContain("<svg");
    expect(html).toContain('aria-label="Verity"');
    expect(html).toContain('viewBox="0 0 36 36"');
  });
});
