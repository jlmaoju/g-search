# 发布检查清单

发布源码前：

- 确认仓库里没有 `.env`。
- 确认仓库里没有 `data/`、`dist/`、`node_modules/`、`.venv*`。
- 确认没有真实 API Key、SSH 私钥、服务器密码、个人域名部署配置。
- 确认 README 里的数据包文件名和实际上传文件一致。
- 确认 `launchers/start_gsearch.ps1` 能从 `.env` 读取 `ZHIPU_API_KEY`。
- 确认 `node --check gcores_crawler/frontend/assets/app.js` 通过。
- 确认 `python -m py_compile` 对核心源码通过。
- 确认 `frontend/` 内运行 `npm ci`、`npm test`、`npm run build` 通过。
- 确认前端构建与已验收版本一致，样例仅含测试数据，开发代理默认连接本地后端。

发布数据包前：

- 确认压缩包解压后根目录是 `data/`。
- 确认包含 `data/catalog.sqlite`。
- 确认包含 `data/reports/search_ui_meta.json`。
- 确认包含 `data/lexicon/alias_map.json`。
- 确认包含 `data/qdrant_release_storage/`。
