# Dify Ubuntu 部署指南

> 适用：Ubuntu 24.04 LTS（同时适用 20.04 / 22.04） | 文档来源：https://github.com/langgenius/dify@main （`README.md`、`docker/README.md`、`docker/.env.example`、`docker/docker-compose.yaml`） | 生成日期：2026-07-08

## 部署方式说明

Dify 官方推荐且最简单的自托管部署方式是 **Docker Compose**（使用官方预构建镜像 `langgenius/dify-*</nginx:latest/postgres/redis/weaviate` 等，直接从 Docker Hub 拉取，**不进行任何源码编译**）。本指南采用此方式。

镜像由项目官方维护并发布在 Docker Hub：https://hub.docker.com/u/langgenius

---

## 1. 环境与依赖

### 系统要求
- OS：Ubuntu 24.04 LTS（也适用 20.04 / 22.04 LTS）
- CPU：>= 2 Core
- 内存（RAM）：>= 4 GiB（官方最低要求）
- 磁盘：>= 20 GiB 可用空间（用于镜像、PostgreSQL 数据、向量库数据、上传文件）
- 端口：默认占用宿主机 **80**（HTTP 入口）、**443**（HTTPS，默认未启用）、**5003**（插件远程调试，可选）。若 80 端口被占用，可通过 `.env` 中 `EXPOSE_NGINX_PORT` 改为其它端口（见第 3 节）。
- 用户：需具有 `sudo` 权限以安装 Docker；运行容器使用 `docker` 组或 `sudo`。

### 安装 Docker 与 Docker Compose

Ubuntu 官方仓库中的 `docker.io` 版本较旧，使用 Docker 官方 apt 源安装最新版 Docker Engine 与 Compose 插件。

```bash
# 1. 安装必要依赖并添加 Docker 官方 GPG key 与 apt 源
sudo apt update
sudo apt install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 2. 安装 Docker Engine、CLI、containerd 与 Compose 插件
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3. 将当前用户加入 docker 组，免去每次 sudo（执行后需重新登录生效）
sudo usermod -aG docker $USER
```

> 重新登录或执行 `newgrp docker` 后即可不带 `sudo` 运行 `docker` 命令。

### 依赖检查

```bash
docker --version
# 期望: Docker version 27.x 或更高，如 "Docker version 27.5.1, build 9f9e405"

docker compose version
# 期望: Docker Compose version v2.x.x （注意是 "docker compose" 子命令，非旧版 docker-compose）

sudo systemctl status docker --no-pager
# 期望: active (running)
```

---

## 2. 获取 Dify 部署文件并启动

### 2.1 克隆官方仓库（仅用于获取 `docker/` 目录的 compose 编排文件）

```bash
# 浅克隆，只取最新代码（不构建、不运行任何安装脚本）
git clone --depth 1 -b main https://github.com/langgenius/dify.git
cd dify/docker
```

> 如需固定版本，可改用 `-b 1.15.0` 检出对应 tag。

### 2.2 准备环境变量文件

```bash
# 当前位于 dify/docker 目录
cp .env.example .env
```

`.env` 包含启动所需全部默认值，默认即可正常启动。可选项见第 3 节。

### 2.3 拉取镜像并启动全部服务

```bash
# 在 dify/docker 目录内执行；首次会从 Docker Hub 拉取若干镜像（约 2~5 GB）
docker compose up -d
```

启动完成后会同时运行以下核心容器：`nginx`（入口）、`api`、`worker`、`worker_beat`、`api_websocket`、`web`（前端）、`db_postgres`、`redis`、`weaviate`（默认向量库）、`sandbox`、`plugin_daemon`、`ssrf_proxy`。

---

## 3. 启动与配置

### 3.1 服务控制命令（在 `dify/docker` 目录内执行）

```bash
# 查看所有容器状态
docker compose ps

# 查看实时日志（全部服务）
docker compose logs -f

# 查看指定服务日志（如 api）
docker compose logs -f api

# 停止全部服务（保留数据）
docker compose down

# 启动全部服务
docker compose up -d

# 升级到新版本：拉取最新代码与镜像后重启
git pull
docker compose pull
docker compose up -d
```

### 3.2 关键配置文件

- 主配置文件：`dify/docker/.env`（由 `.env.example` 复制而来）
- 高级/可选配置（按主题分文件）：`dify/docker/envs/*.env.example`，使用时复制为同名 `.env`（去掉 `.example` 后缀）
- Nginx 模板：`dify/docker/nginx/nginx.conf.template`、`dify/docker/nginx/conf.d/default.conf.template`
- 数据卷默认位置：`dify/docker/volumes/`（PostgreSQL、Redis、Weaviate、sandbox、plugin 等持久化数据）

### 3.3 必知/常改配置项（编辑 `dify/docker/.env`，修改后需 `docker compose up -d` 重建）

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `EXPOSE_NGINX_PORT` | `80` | 对外暴露的 HTTP 端口；宿主机 80 被占用时改为如 `8080` |
| `EXPOSE_NGINX_SSL_PORT` | `443` | 对外暴露的 HTTPS 端口 |
| `NGINX_HTTPS_ENABLED` | `false` | 是否启用 HTTPS；启用需提供证书文件 `nginx/ssl/dify.crt` 与 `dify.key` |
| `INIT_PASSWORD` | （空） | 控制台首次访问的管理员初始密码；不设则在浏览器向导中设置 |
| `SECRET_KEY` | （空=自动生成） | 用于签发 session/JWT/文件 URL；留空时自动生成并持久化在存储目录；多副本部署需显式指定同一值 |
| `CONSOLE_API_URL` | （空） | 控制台 API 对外公开 URL，如 `https://dify.example.com`；生产建议填写实际域名 |
| `CONSOLE_WEB_URL` | （空） | 控制台 Web 对外公开 URL |
| `SERVICE_API_URL` | （空） | 对外暴露给应用调用的 Service API URL |
| `APP_WEB_URL` | （空） | 应用端 Web URL |
| `VECTOR_STORE` | `weaviate` | 向量库类型；可选 `weaviate`、`milvus`、`qdrant`、`pgvector`、`elasticsearch`、`opensearch`、`chroma` 等（详见 `envs/vectorstores/`） |
| `DB_PASSWORD` | `difyai123456` | PostgreSQL 密码；**生产环境务必修改** |
| `REDIS_PASSWORD` | `difyai123456` | Redis 密码；**生产环境务必修改** |
| `STORAGE_TYPE` | `opendal` | 文件存储类型，默认本地 `opendal`/`fs`；可改为 S3、Azure Blob 等（见 `envs/`） |
| `LOG_LEVEL` | `INFO` | 日志级别，排障时可改 `DEBUG` |

> 修改 `.env` 后必须执行 `docker compose up -d` 让变更生效；修改了镜像或 compose 文件时需先 `docker compose pull`。

### 3.4 防火墙放行（如启用 ufw）

```bash
# 放行 HTTP 入口（默认 80；若改了 EXPOSE_NGINX_PORT 请相应替换）
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp    # 若启用 HTTPS
sudo ufw reload
```

> 若云厂商有安全组，需在控制台同步放行对应端口。

---

## 4. 访问入口

服务全部启动后，在浏览器访问：

```
http://<服务器IP>/install
```

本地部署即 `http://localhost/install`。首次访问会进入**初始化向导**：设置管理员邮箱、用户名、密码，完成后进入 Dify 控制台。

---

## 5. 常见部署期问题

| 现象 | 排查与解决 |
|------|-----------|
| `docker compose up -d` 报端口占用 | 80/443 被占用。修改 `.env` 中 `EXPOSE_NGINX_PORT` 与 `EXPOSE_NGINX_SSL_PORT` 为空闲端口，再 `docker compose up -d` |
| 容器启动后访问 `http://localhost` 502 | `api` 或 `web` 未就绪。运行 `docker compose ps` 查看是否 `unhealthy`，`docker compose logs api` 查看错误 |
| 内存不足导致 `worker`/`api` 被 OOM Kill | 至少保证 4 GiB 内存；可在 `.env` 调小 `CELERY_WORKER_AMOUNT`（默认 4）和 `SERVER_WORKER_AMOUNT`（默认 1） |
| 拉取镜像慢/失败 | 配置 Docker 镜像加速器（阿里云、daocloud 等）；或使用代理 |
| 首次访问 `/install` 跳到 `/login` 且无初始化界面 | 表示已被初始化过；如需重置需清空 `volumes/db_data`、`volumes/storage` 后重新 `docker compose up -d`（**会丢失全部数据**） |
| 修改 `.env` 不生效 | 必须执行 `docker compose up -d`；某些变量（如 `DB_PASSWORD`）变更后需重建数据库容器并可能涉及数据迁移 |
| 向量库切换后知识库报错 | 切换 `VECTOR_STORE` 后旧知识库需重新索引；各向量库连接参数见 `envs/vectorstores/` |

> 安装完成后的验证方法见独立文档：`dify-verify.md`
