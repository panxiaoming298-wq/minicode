# Official DeepSeek tokenizer data

Source: https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip

Official usage guide: https://api-docs.deepseek.com/quick_start/token_usage/

Downloaded 2026-09-22. Archive SHA256: `e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`.

`deepseek-v4.json` is the unmodified `deepseek_v4_tokenizer/tokenizer.json` data;
SHA256 `89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614`.
Runtime does not execute downloaded Python or load remote code. The separately
published tokenizer chat template is not used to claim exact API framing.

This file supports a conservative, calibrated JSON input estimate for the exact
official `deepseek-flash` route. Unknown routes/formats, special-token text,
missing dependencies and identity errors use the original UTF-8 byte estimate.
Provider-reported usage remains the accounting authority. A detected low
estimate disables this optimization for the run, including after restoration.

Enable explicitly with `runtimeTokenEstimation: "official-tokenizer"` for primary
runs or `MINI_CODE_RUNTIME_TOKEN_ESTIMATION=official-tokenizer`. The default is
`utf8-bytes`. Install the project's `web` extra to include the `tokenizers`
runtime; missing optional dependencies cause conservative fallback.
