# Dify 安装验证指南

> 适用：Ubuntu 24.04 LTS（同时适用 20.04 / 22.04） | 配套部署指南：`dify-install.md` | 文档来源：https://github.com/langgenius/dify@main | 生成日期：2026-07-08

本文档用于在 Dify 通过 Docker Compose 部署完成后，逐项验证安装是否成功。**所有命令在宿主机 `dify/docker` 目录下执行**。按顺序完成以下 6 项检查，全部通过即视为安装完成并可用。

---

## 1. 部署文件与版本

```bash
# 进入部署目录（路径按实际部署位置调整）
cd ~/dify/docker   # 或 git clone 后的 dify/docker

# 确认 compose 编排文件存在
ls docker-compose.yaml .env
# 期望: 列出 docker-compose.yaml 与 .env 两个文件，无 "No such file" 错误
```

> Dify 服务以容器形式运行，没有单一宿主机二进制；版本通过镜像标签或控制台「关于」页确认（见第 5 节）。

---

## 2. 容器与服务状态

```bash
# 列出所有容器及其状态
docker compose ps
# 期望: 至少以下服务为 Up 状态：
#   nginx, api, worker, worker_beat, api_websocket, web,
#   db_postgres, redis, weaviate, sandbox, plugin_daemon, ssrf_proxy
# 关键要求：
#   - nginx、api、web、db_postgres、redis 行的 STATUS 列显示 "Up ..." 且无 "(unhealthy)"
#   - 容器数量通常为 12 个左右（不含默认禁用的向量库变体）

# 仅查看是否有非运行状态的容器
docker compose ps --format '{{.Service}}\t{{.Status}}' | grep -v -E 'Up'
# 期望: 无输出（表示所有服务都在运行）
```

---

## 3. 端口与监听

```bash
# 默认 HTTP 入口端口 80（若 .env 中改过 EXPOSE_NGINX_PORT 请相应替换）
ss -tlnp | grep ':80 '
# 期望: 有一行 LISTEN 0.0.0.0:80 或 [::]:80，进程为 docker-proxy / dockerd 相关

# 验证 HTTPS 端口（仅当 NGINX_HTTPS_ENABLED=true 时）
ss -tlnp | grep ':443 '
# 期望: LISTEN 443

# 验证插件调试端口（默认开启，可选检查）
ss -tlnp | grep ':5003 '
# 期望: LISTEN 5003
```

---

## 4. HTTP 入口可达性

```bash
# 检查 Nginx 入口根路径（应返回 200，未初始化时访问 /install）
curl -i -f http://localhost/ | head -1
# 期望: HTTP/1.1 200

# 检查初始化向导页面
curl -i -f http://localhost/install | head -1
# 期望: HTTP/1.1 200 （或 302 跳转到 /install、/login 均属正常）

# 检查控制台 Web 首页 HTML（应含 Dify 前端资源）
curl -s http://localhost/ | grep -oiE '<title>[^<]*</title>'
# 期望: 输出包含 "Dify" 字样，如 <title>Dify</title> 或包含 Dify 的标题
```

> 也可直接在浏览器访问 `http://<服务器IP>/install`，应看到 Dify 初始化向导或登录页。

---

## 5. 核心服务功能性验证

### 5.1 PostgreSQL 数据库（`db_postgres`）

```bash
# 通过 compose 在 db_postgres 容器内执行 psql
docker compose exec -T db_postgres psql -U postgres -d dify -c '\dt' | head -20
# 期望: 输出 Dify 业务表列表，至少包含 schemas_migrations、accounts、apps、datasets 等多张表
#       （表数量通常 > 50 张）
```

### 5.2 Redis（`redis`）

```bash
docker compose exec -T redis redis-cli -a difyai123456 ping
# 期望: PONG
# （-a 后的密码来自 .env 中 REDIS_PASSWORD，默认 difyai123456；若改过密码请同步替换）
```

### 5.3 Weaviate 向量库（`weaviate`，默认）

```bash
# Weaviate 仅在容器网络内暴露，通过 compose exec 访问
docker compose exec -T weaviate sh -c 'wget -qO- http://localhost:8080/v1/.well-known/ready'
# 期望: 输出 true 或 HTTP 200 响应
```

### 5.4 API 健康检查

```bash
# 直接访问 api 容器内的健康端点
docker compose exec -T api curl -sf http://localhost:5001/health
# 期望: 输出 "ok" 或类似 200 响应
```

---

## 6. 日志检查

```bash
# 查看 api 与 nginx 最近 50 行日志，确认无 ERROR / FATAL / Traceback
docker compose logs --tail=50 api | grep -iE 'ERROR|FATAL|Traceback|Exception'
# 期望: 无输出，或仅出现可忽略的告警（如连接重试后已恢复）

docker compose logs --tail=50 nginx | grep -iE '\b5[0-9]{2}\b|error'
# 期望: 无持续 5xx 错误

# 查看 worker（异步任务）日志
docker compose logs --tail=50 worker | grep -iE 'ERROR|Traceback'
# 期望: 无输出
```

> 首次启动时 `api` 与 `worker` 会先执行数据库迁移（`MIGRATION_ENABLED=true`），耗时 1~3 分钟，期间日志中出现迁移 SQL 属正常。

---

## 7. 浏览器端最终确认（手工）

1. 浏览器访问 `http://<服务器IP>/install`。
2. 首次访问：按向导填写管理员邮箱、用户名、密码，点击注册/初始化。
3. 成功后进入 Dify 控制台首页（工作区），能看到「创建空白应用」「从模板创建」等按钮，即视为前端、API、数据库、Redis 链路全部正常。
4. （可选）进入「设置 → 模型供应商」配置一个 LLM（如 OpenAI），创建一个聊天应用并完成一次对话，验证 LLM 调用与向量库链路。

---

## 判定标准

满足以下**全部条件**即视为 Dify 安装完成并可用：

- 第 2 项：`docker compose ps` 中核心服务（nginx、api、worker、api_websocket、web、db_postgres、redis、weaviate、sandbox、plugin_daemon）均为 `Up` 且无 `(unhealthy)`。
- 第 3 项：宿主机 80 端口（或自定义的 `EXPOSE_NGINX_PORT`）处于 LISTEN。
- 第 4 项：`curl -i -f http://localhost/install` 返回 `HTTP/1.1 200`（或 302）。
- 第 5 项：PostgreSQL、Redis、Weaviate、API 四项功能性命令均符合期望输出（表已创建、PONG、ready=true、health=ok）。
- 第 6 项：核心服务日志中无持续的 ERROR / Traceback。

第 7 项为端到端的人工最终确认，建议执行以确保 Web 控制台可正常初始化与登录。
