# 可选：后端服务容器化部署（Ollama / AnythingLLM）

主程序本身**不需要 Docker**，双击就能用。这里只给「想在本机跑 Ollama 或
AnythingLLM 做 AI 后端」的进阶用户提供容器配置，属于可选增强模块。

## 启动

```bash
cd deploy
docker compose up -d
```

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| Ollama | `http://127.0.0.1:11434` | 本地大模型；设置页 Base URL 填这个地址 |
| AnythingLLM | `http://127.0.0.1:3001` | 知识库 / 文档问答，不参与待办抽取 |

拉一个模型：

```bash
docker compose exec ollama ollama pull qwen2.5:7b
```

然后在 Chat2Todo 设置页 → AI：
- 预设选「Ollama（本地）」
- Base URL `http://127.0.0.1:11434/v1`
- API Key 随便填（Ollama 不校验，但不能为空）
- 点「测试连接」确认绿色通过

## 连通性排查

| 现象 | 排查 |
| --- | --- |
| 设置页「测试连接」超时 | `curl http://127.0.0.1:11434/api/tags`；容器没起来看 `docker compose logs ollama` |
| 401 / 403 | 填了错误的 API Key；Ollama 任意值即可，云端服务要真实 key |
| 404 | Base URL 少写或多写 `/v1`；OpenAI 兼容端点要带 `/v1` |
| 端口被占 | 本机已装 Ollama 客户端时先 `net stop` 或改 `ports` 映射 |
| 中文乱码 | 模型选中文友好的（qwen2.5 / glm4），小参数模型对中文待办识别差 |

## 安全边界

- 容器只映射到 `127.0.0.1`，不要改成 `0.0.0.0`；
- Chat2Todo 只对本机 / 内网地址放行明文 http，公网地址必须 https；
- 发往 AI 的内容已按脱敏规则处理，白名单群在这里**不**豁免。
