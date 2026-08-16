import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

/**
 * eslint-config-next is deliberately absent: its rushstack patch does not load
 * under ESLint 9 flat config. The rule that actually protects correctness —
 * react-hooks — is applied directly, and Next's own build performs its
 * framework checks.
 */
export default tseslint.config(
  {
    ignores: [
      ".next/**",
      ".next-build/**",
      "node_modules/**",
      "next-env.d.ts",
      "*.config.mjs",
      "*.config.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      globals: { window: "readonly", document: "readonly", fetch: "readonly", FormData: "readonly",
                 File: "readonly", Response: "readonly", DOMException: "readonly", console: "readonly",
                 process: "readonly", Intl: "readonly", sessionStorage: "readonly", AbortSignal: "readonly" },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // PRD FR-FE-001: components stay small and hold no business logic.
      "max-lines": ["error", { max: 250, skipBlankLines: true, skipComments: true }],
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/consistent-type-imports": "error",
    },
  },
);
