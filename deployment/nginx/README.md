# 网页部署与封面代理

这是通用配置示例，需按自己的站点目录、后端端口和系统环境调整。新版 `frontend/` 默认将 `image.gcores.com` 图片交给 `/media/gcores/`；旧前端在 `assets/runtime-config.json` 设置 `"imageProxyBase": "/media/gcores"` 后使用同一路由。未配置的本地 Viewer 继续使用 `/api/media-asset`。

新版前端执行 `npm run build` 后，将 `frontend/dist/client/` 的内容放到自己的静态站点目录，例如 `/var/www/gsearch`。在已配置域名和 TLS 的 server 块中加入：

```nginx
root /var/www/gsearch;
index index.html;
include /etc/nginx/snippets/gsearch-image-location.conf;

location /api/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_read_timeout 40s;
}
location /assets/ {
    try_files $uri =404;
    expires 1h;
}
location / {
    try_files $uri $uri/ /index.html;
    add_header Cache-Control "no-cache";
}
```

开发和本地构建预览由 Vite 直接代理到本地后端，无需 Nginx；生产静态站点需要上述同源接口。

部署位置：

- `gsearch-image-cache.conf` → `/etc/nginx/conf.d/gsearch-image-cache.conf`（http 上下文）。
- `gsearch-image-location.conf` → `/etc/nginx/snippets/gsearch-image-location.conf`。
- 在自己的站点 server 块内添加 `include /etc/nginx/snippets/gsearch-image-location.conf;`。
- 缓存目录 `/var/cache/nginx/gsearch-images` 应由 Nginx worker 用户写入，例如 `www-data`。缓存上限 512 MB。

示例使用 systemd-resolved 的 `127.0.0.53`；部署前应核对 resolver 和 CA 文件路径。上线前运行 `nginx -t`，成功后 reload，然后启用前端配置。保留旧配置和旧前端版本用于回滚。

该路由固定访问 `https://image.gcores.com`，不接收任意上游域名，开启 SNI、证书链及主机名验证；不向上游转发用户 Cookie、Authorization 或请求体。成功图片缓存，已缓存图片可在上游短暂失败时继续使用。图片响应带有 CSP sandbox。

验证真实图片应同时检查 HTTP 200、`Content-Type: image/*`、非空响应及浏览器解码尺寸；重复请求应出现 `X-GSearch-Image-Cache: HIT`。不要只看网页和健康检查返回 200。

配置依据：[Nginx proxy 模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[Nginx resolver](https://nginx.org/en/docs/http/ngx_http_core_module.html#resolver)。
