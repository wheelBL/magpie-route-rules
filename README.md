# Magpie Route Rules

请求感知的路由规则，作为 [Magpie](https://github.com/yetone/magpie) 的**网关中间件**（gateway middleware）：在 magpie 选择 provider/成员之前，按规则把代理请求的模型改写为另一个模型——对任意 provider、任意路由组生效。

- 带图请求 → 指定视觉模型（避免发到不支持图片的模型上撞 406）
- 输入长度 ≥ N → 指定长上下文/强模型
- 输出预算 ≥ N → 指定输出容量足够的模型
- 可选的每模型输出钳制（limits），把超过上限的 `max_tokens` 收敛后再发

与 magpie 路由组的关系：组规则（images/tokens）+ 失败兜底适合"一个组内选成员"；本中间件适合"跨组/跨 provider 的请求改写"，两者可并存——中间件在组路由之前运行，先改写模型，组规则看到的已是改写后的模型。不要把同一套规则同时配在两处。

## 安装

```powershell
magpie plugin add "C:\path\to\magpie-route-rules"
```

中间件文件变更会在 ~1 秒内被下一个请求拾取（无需重启）；规则本身通过 options 配置：

```powershell
magpie plugin options magpie-route-rules "{\"rules\":[{\"type\":\"images\",\"target\":\"cannbot/deepseek-v4.1-flash\"},{\"type\":\"output\",\"at\":4096,\"target\":\"cannbot/glm-5.3\"}],\"limits\":{\"cannbot/deepseek-v4.1-flash\":4096}}"
```

## Options

```json
{
  "rules": [
    { "type": "images", "target": "cannbot/deepseek-v4.1-flash" },
    { "type": "input", "at": 100000, "target": "cannbot/deepseek-v4-pro" },
    { "type": "output", "at": 4096, "target": "cannbot/glm-5.3" }
  ],
  "limits": { "cannbot/deepseek-v4.1-flash": 4096 },
  "clamp": true,
  "scope": ["group/steady"]
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `rules` | `[]` | 按序评估，首中即改写 `model` 为 `target`。`images`：请求带图片部件；`input`：估算输入 token ≥ `at`（ASCII≈4 字符/token，CJK≈1.1，图片数据不计）；`output`：请求输出预算 ≥ `at`。`target` 是 magpie 模型 id（`provider/model` 或 `group/<id>`） |
| `limits` | `{}` | 每模型输出上限（键为代理所见的模型 id），超限的 `max_tokens`/`max_completion_tokens` 收敛后再发 |
| `clamp` | `true` | 是否应用 `limits` 钳制 |
| `scope` | 全部 | 可选；数组元素为精确模型 id 或 `/正则/`，仅匹配的所问模型参与规则 |

## 行为细节

- **逐请求**：每个请求独立评估；回复（SSE 事件与 JSON）中的模型名会还原为代理所问的名称，读回模型名的代理（如 Claude Code）看到的仍是它请求的名字。
- **fail-open**：任何钩子出错/超时（onRequest 250ms）/返回非法值，请求按原样放行——规则坏了不会挡流量。
- **无状态**：中间件钩子按设计无跨请求状态，图片能力学习、吞吐率统计不在本层（跨成员兜底由路由组的失败转移承担）。
- **纯 JS 无依赖**：中间件运行在 magpie 网关自带的 JS 引擎里，只允许相对导入，本文件零导入。
- 验证：`node --test test/`（16 项：规则命中/估算/scope/钳制/名字还原/fail-open）。

## 许可

MIT。

## 自发现与自学习（route-learn 伴生）

中间件按 magpie 设计是无状态的——发现与学习由 [tools/route-learn.py](./tools/route-learn.py) 伴生脚本承担：

- **自举播种**：`python tools/route-learn.py --bootstrap`——新机器零配置：从网关 `/v1/models` 与 usage 日志里最近成功的成员自动生成 images 规则候选链（排除组 id 自环），并注册每 5 分钟的学习计划任务。仓库里 package.json 的 options 只是编辑器示例占位，不是生效规则。
- **持续学习**：此后每 5 分钟增量读 magpie usage 日志——图片能力负学习（406/multimodal → 标 bad 6h，带图请求跳过）、失败动作（499→ignore、`expected a value <= N`→clampOutput 跟随声明值、429/5xx→pause 指数退避 2min×2^连败、任何 200 立即解除 pause）、未知签名经网关 AI 分类兜底（模型从历史成功/可用列表自动选择，不写死）。
- **原生视觉 vs 代看图（0.3.44）**：带图请求的 200 不等于模型自己看得见图——magpie 会让 Image recognition 池的模型把图描述成文字再喂给无视觉模型。探针改用每次唯一的随机小图，并把 vision-cache.jsonl 的前后差异当作信号：出现新条目 = 走了代看链路 = 模型原生无视觉 → 标 `via:'describe'`；describer 池耗尽时 502 文案直接点名（`model X can't see images`），按名归因。via=describe 的标记不再按 6h TTL 衰减（中间件视其长期 incapable），改为每周复核，避免把无视觉模型排在有视觉候选前面；只有 200 且未走代看才标 good。
- 学习结果写回本插件的 options（learned.images / learned.actions），中间件热拾取后按当前信念执行。

新机器一条命令完成自举：`python tools/route-learn.py --bootstrap`
