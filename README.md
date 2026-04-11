# G-Search (Public Export)

这是从私有工作区导出的精简公开版仓库，包含：

- 机核公开内容抓取
- 转录/索引准备
- 本地向量检索
- Viewer 查询与打包

## 仓库内容

- `gcores_crawler/`：核心抓取、索引、搜索、Viewer 代码
- `scripts/`：精选后的通用辅助脚本，主要用于预览库构建、ASR wrapper、Viewer/Search 辅助
- `packaging/`：Windows Viewer 的打包入口与 spec
- `launchers/`：少量可公开使用的 PowerShell 启动脚本

## 不包含的内容

- `data/` 下的本地/私有数据
- `dist/` 下的构建产物
- 虚拟环境、vendor 下载物、缓存文件
- 个人 keepalive 配置、托盘工具、本地日志、内部自动化产物
- 大部分运维/报表/对账脚本
- 任何硬编码 API Key

## 密钥约定

所有 API Key 都必须通过环境变量或显式命令参数提供。

常用环境变量：

- `ZHIPU_API_KEY`
- `GCORES_QDRANT_URL`
- `GCORES_QDRANT_API_KEY`
- `GCORES_QDRANT_COLLECTION`
- `GSEARCH_VIEWER_FALLBACK_API_KEY`
- `VOLCENGINE_APP_ID`
- `VOLCENGINE_ACCESS_KEY`

可以参考 `.env.example`。

## 快速开始

发现和抓取内容：

```bash
python -m gcores_crawler discover articles --limit 5
python -m gcores_crawler fetch radios 212377
```

构建本地搜索库：

```bash
python -m gcores_crawler build-search-index --output-dir data --qdrant-path qdrant
```

启动搜索 UI：

```bash
python -m gcores_crawler serve-search --output-dir data --qdrant-path qdrant --host 127.0.0.1 --port 8765
```

导出 query-only Viewer 包：

```powershell
powershell -ExecutionPolicy Bypass -File .\launchers\build_viewer_windows_release.ps1
```

## 说明

- 这个公开版仍然偏 Windows 工作流，尤其是 Viewer 打包相关脚本。
- 部分功能依赖本地准备好的 SQLite 数据和运行中的 Qdrant。
- `requirements-public.txt` 只是一个实用起点，不是严格锁版本环境。
- 内部监控、对账、日常运维脚本已经刻意从公开版移除。

## 发布前建议

- 检查 `PUBLISH_CHECKLIST.md`
- 确认许可证是否符合你的预期
- 私有工作区有更新时，重新导出一次公开版
