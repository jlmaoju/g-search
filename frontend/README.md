# Gsearch 网页前端

线上红白版前端的 React 源码。包含空白欢迎首屏、内容 / 标题 / 时间轴三种检索方式、完整节目类型和参与者筛选、上下文与逐字稿、结果排序、GitHub 入口，以及网络故障、后端离线、超时、限流和语义检索额度不足提示。

首次进入不提交搜索；输入线索并提交后才调用检索接口。点击字标返回欢迎页。

## 本地运行

先按照仓库根目录 README 准备数据和后端，默认地址为 `http://127.0.0.1:8765`。然后在本目录运行（需要 Node.js 22.12+）：

```powershell
npm ci
npm run dev
```

打开 Vite 输出的本地地址。开发服务器将 `/api/` 和封面请求转发到自己的后端，不需要在前端填写 API Key。后端地址可通过环境变量覆盖，例如 PowerShell：

```powershell
$env:GSEARCH_API_TARGET = 'http://127.0.0.1:9000'
npm run dev
```

`GSEARCH_API_TARGET` 仅供开发和本地构建预览使用，不会写入客户端代码。该前端使用现有搜索 API；本地 Viewer 的 Key 配置和启动逻辑仍在 `gcores_crawler/frontend/`。

## 测试与构建

```powershell
npm test
npm run build
npm run preview
```

构建产物位于 `dist/client/`。源码和锁文件入库，依赖、构建产物和检索数据库不入库。

API 回归测试使用人工编写的样例和模拟响应，不包含真实节目转录，不调用付费服务。浏览器故障验证可在构建后运行：

```powershell
npm run qa:faults
```

打开 `http://127.0.0.1:18766/` 选择测试场景。覆盖离线与恢复、30 秒超时、限流、额度不足、关键词降级、空结果、错误响应、筛选失败、图片失败和 200 条结果上限。每个场景初始应显示欢迎页，手动提交查询后出现相应状态。`/__qa/stats` 可查看搜索请求次数，用于确认欢迎页、切换方式和筛选不会自动搜索。

## 部署

将 `dist/client/` 作为网站根目录，并在同源提供：

- `/api/`：转发到自己的搜索后端。
- `/media/gcores/`：机核图片代理，参考 [`deployment/nginx/`](../deployment/nginx/README.md)。

该目录提供通用 Nginx 配置片段，不包含线上服务器地址、证书或运维凭据。部署时保留旧版静态资源用于缓存兼容，并保留上一版目录便于回滚。

## 素材

`public/assets/` 仅收录项目字标和红色引号。节目封面从 API 返回的机核链接加载。图标使用 Remix Icon，数字字体使用 Oswald，随 npm 依赖获取；各依赖许可见其包内 LICENSE。
