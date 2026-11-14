# Codex Workbench 前端

工作台 SPA(React + Vite),构建产物由 bridge 的 Node 进程在同源路径 `/workbench/` 下静态托管。

## 构建

```bash
npm install --registry=https://registry.npmmirror.com
npm run build          # 即 tsc -b && vite build
```

- 产物路径:`web/workbench/dist/`(`vite.config.ts` 中 `base: '/workbench/'`,勿改)
- 构建前先装依赖;新增依赖请同样走上面的镜像源。

## 上线部署

`dist/` 被 `.gitignore` 忽略,**不入库**,部署方式为本地构建后经 scp 同步到服务器:

```bash
# 本地(在 web/workbench 下构建完成后)
scp -r dist/* <user>@<server>:<bridge部署目录>/web/workbench/dist/
```

上线前置要求:

1. 服务器上对应目录必须存在最新的 `web/workbench/dist`(缺失则 `/workbench/` 无法访问);
2. 所有 API 走相对路径 `/workbench/api`,构建产物不含任何绝对地址,同步后可直接运行,无需改配置;
3. 若后续改用"产物入库"策略,需同步调整 `.gitignore`(当前刻意不改,部署统一走 scp)。

## 本地开发

```bash
npm run dev
```

开发服务器把 `/workbench/api` 反代到真实 bridge 实例,默认指向本机
`http://127.0.0.1:28080`;真实部署地址请用环境变量 `WORKBENCH_API_TARGET`
提供(源码中不硬编码生产地址):

```powershell
$env:WORKBENCH_API_TARGET='http://<your-server-host>:28080'; npm run dev
```

即本地调试时前端始终与"同源部署"的线上布局一致,无需 CORS 配置。

## 安全说明

`.docx` 预览由 mammoth 转 HTML 后经 `dompurify` 净化再注入(见
`src/components/Preview.tsx` 的 `sanitizeDocxHtml`),可拦截
`javascript:` 等危险协议的超链接,防止点击触发同源 XSS。
