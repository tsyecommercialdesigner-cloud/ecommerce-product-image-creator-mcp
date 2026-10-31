# Models
- gemini-3.1-flash-lite-image 支持 [Gemini Official Format, OpenAI Chat Completions]
- nano-banana-pro 仅支持 [OpenAI Dall-e Format: Generations, OpenAI Dall-e Format: Edits]
- gemini-nano-banana-2.1 支持 [Gemini Official Format, OpenAI Chat Completions, OpenAI Dall-e Format: Generations]

本页以用户于 2026-10-20（Asia/Shanghai）补充的支持范围为准：

| 模型 | Gemini Official Format | OpenAI Chat Completions | Dall-e Generations | Dall-e Edits |
| --- | --- | --- | --- | --- |
| gemini-3.1-flash-lite-image | 支持 | 支持 | 不支持 | 不支持 |
| nano-banana-pro | 不支持 | 不支持 | 支持 | 支持 |
| gemini-nano-banana-2.1 | 支持 | 支持 | 支持 | 不支持 |

原接口名称 `gemini-2.5-pro:generateContent` 统一更正为 **Gemini Official Format**；实际请求仍使用 `/v1beta/models/{model}:generateContent`，将 model 替换为该格式支持的模型名。`generationConfig` 及所列嵌套配置均非必填。

`nano-banana-pro` 仅使用 OpenAI Dall-e Format（Generations 与 Edits），图片结果返回 URL。按本次补充说明，**官方返回空图导致的失败不扣费**；这是补充的服务行为说明，历史测试未独立核验账单。

历史共执行 43 次 API 探针（1 GET、42 POST），原始脱敏请求/响应保留在[实测报告](validation/2026-10-20/REPORT.md)。其中部分已明确不支持的模型与接口组合也曾返回 HTTP 200；这些结果仅作历史观察，不作为正式支持依据，也不作为当前请求示例。本次支持范围修订未新增 API 调用。

请求基址为 `https://ai.t8star.org`。`{{YOUR_API_KEY}}`、`{{MODEL}}`、`{{PROMPT}}` 使用前需替换；各节分别列明允许的模型。Python `http.client` 使用 `HTTPSConnection("ai.t8star.org")` 加相对请求路径。所有真实响应中的图片 URL / Base64 与不透明签名已脱敏，占位符不能用于下载或解码。

# Gemini Official Format

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /v1beta/models/{model}:generateContent:
    post:
      summary: Gemini Official Format
      deprecated: false
      description: Gemini Official Format。当前支持 gemini-3.1-flash-lite-image、gemini-nano-banana-2.1；不支持 nano-banana-pro。路径中的
        model 替换为所选模型名；generationConfig 及所列嵌套配置均非必填。
      tags:
      - 聊天(Chat)/Gemini Official Format
      parameters:
      - name: model
        in: path
        required: true
        description: 将当前受支持的模型名放入路径。
        schema:
          type: string
          enum:
          - gemini-3.1-flash-lite-image
          - gemini-nano-banana-2.1
      - name: Content-Type
        in: header
        description: ''
        required: true
        example: application/json
        schema:
          type: string
      - name: Authorization
        in: header
        description: ''
        required: false
        example: Bearer {{YOUR_API_KEY}}
        schema:
          type: string
          default: Bearer {{YOUR_API_KEY}}
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                contents:
                  type: array
                  items:
                    type: object
                    properties:
                      parts:
                        type: array
                        items:
                          type: object
                          properties:
                            text:
                              type: string
                generationConfig:
                  type: object
                  properties:
                    thinkingConfig:
                      type: object
                      properties:
                        thinkingBudget:
                          type: integer
                        includeThoughts:
                          type: boolean
                  description: 可选；thinkingConfig、thinkingBudget、includeThoughts 也均非必填。
              required:
              - contents
            example:
              contents:
              - parts:
                - text: A cat.
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 聊天(Chat)/Gemini Official Format
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-350079690-run
components:
  schemas: {}
  securitySchemes: {}
servers:
- url: https://ai.t8star.org
security: []
```

**支持范围：** `gemini-3.1-flash-lite-image`、`gemini-nano-banana-2.1`；`nano-banana-pro` 不支持此格式。

路径为 `/v1beta/models/{model}:generateContent`，模型名放在路径中，不需要额外 body model。原固定 `/v1beta/models/gemini-2.5-pro:generateContent` 属于另一个模型路径，不能代替本项目的模型选择。

`generationConfig`、`thinkingConfig`、`thinkingBudget`、`includeThoughts` 均非必填。历史对 `gemini-3.1-flash-lite-image` 的省略、空配置和单字段配置均取得 200；配置的实际思考效果未独立验证。证据：[实测记录 `gemini-0`](validation/2026-10-20/gemini-0.json)、[实测记录 `gemini-0-empty-config`](validation/2026-10-20/gemini-0-empty-config.json)、[实测记录 `gemini-0-budget-only`](validation/2026-10-20/gemini-0-budget-only.json)、[实测记录 `gemini-0-thoughts-only`](validation/2026-10-20/gemini-0-thoughts-only.json)。

## Request Example

`{{MODEL}}` 选择 `gemini-3.1-flash-lite-image` 或 `gemini-nano-banana-2.1`。

Python(Requests):

```python
import requests
import json

url = "https://ai.t8star.org/v1beta/models/{{MODEL}}:generateContent"
payload = json.dumps({
   "contents": [
      {
         "parts": [
            {
               "text": "A cat."
            }
         ]
      }
   ]
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

response = requests.request("POST", url, headers=headers, data=payload)
print(response.text)
```

Python(http.client):

```python
import http.client
import json

conn = http.client.HTTPSConnection("ai.t8star.org")
payload = json.dumps({
   "contents": [
      {
         "parts": [
            {
               "text": "A cat."
            }
         ]
      }
   ]
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

conn.request("POST", "/v1beta/models/{{MODEL}}:generateContent", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
conn.close()
```

JavaScript:

```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "contents": [
      {
         "parts": [
            {
               "text": "A cat."
            }
         ]
      }
   ]
});
const requestOptions = {
   method: "POST",
   headers: myHeaders,
   body: raw,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1beta/models/{{MODEL}}:generateContent", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

## Response Example

以下为 `gemini-3.1-flash-lite-image` 的真实响应，HTTP 200。Base64 与不透明 thoughtSignature 已替换为长度及 SHA-256 占位符，其他字段保留。图片位于 `candidates[].content.parts[].inlineData`，解码后为 JPEG 1408×768。证据：[实测记录 `gemini-0`](validation/2026-10-20/gemini-0.json)。

```json
{
  "candidates": [
    {
      "content": {
        "parts": [
          {
            "inlineData": {
              "mimeType": "image/jpeg",
              "data": "[BINARY_OMITTED length=482700 sha256=b4d92041db2b525bbd320e8dd11fdb622df15e1630baa0de047c8522036336a4]"
            },
            "thoughtSignature": "[OPAQUE_VALUE_OMITTED length=658584 sha256=bb41d3506f3b967b51e5defeabb4d3762c3136a469bf02e0b14030e7a70f4e41]"
          }
        ],
        "role": "model"
      },
      "finishReason": "STOP",
      "index": 0
    }
  ],
  "usageMetadata": {
    "promptTokenCount": 20,
    "candidatesTokenCount": 1368,
    "totalTokenCount": 1388,
    "promptTokensDetails": [
      {
        "modality": "TEXT",
        "tokenCount": 20
      }
    ],
    "candidatesTokensDetails": [
      {
        "modality": "IMAGE",
        "tokenCount": 1120
      }
    ],
    "serviceTier": "standard"
  },
  "modelVersion": "gemini-3.1-flash-lite-image",
  "responseId": "2C3XauTyJJzgytMPoJ7wkQw"
}
```

`gemini-nano-banana-2.1` 使用 `A cat.` 的真实响应同样为 HTTP 200；图片为 JPEG 2752×1536。应遍历 parts 并读取 inlineData 的 MIME 和 Base64，不承诺其他请求的输出尺寸。证据：[实测记录 `followup-gemini-2-simple`](validation/2026-10-20/followup-gemini-2-simple.json)。

```json
{
  "candidates": [
    {
      "content": {
        "role": "model",
        "parts": [
          {
            "inlineData": {
              "mimeType": "image/jpeg",
              "data": "[BINARY_OMITTED length=4025396 sha256=64fb713e5f9353497afcfd8727c2f4404bf7e52cf18519da015873d39c6ed469]"
            }
          }
        ]
      },
      "finishReason": "STOP",
      "index": 0
    }
  ],
  "usageMetadata": {
    "promptTokenCount": 4,
    "candidatesTokenCount": 2050,
    "totalTokenCount": 2054,
    "promptTokensDetails": [
      {
        "modality": "TEXT",
        "tokenCount": 4
      }
    ],
    "candidatesTokensDetails": [
      {
        "modality": "IMAGE",
        "tokenCount": 1680
      },
      {
        "modality": "TEXT",
        "tokenCount": 370
      }
    ]
  },
  "modelVersion": "gemini-nano-banana-2.1"
}
```

# OpenAI Chat Completions

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /v1/chat/completions:
    post:
      summary: OpenAI Chat Completions
      deprecated: false
      description: OpenAI Chat Completions。当前支持 gemini-3.1-flash-lite-image、gemini-nano-banana-2.1；不支持 nano-banana-pro。以下通用参数保留原始规范与适用模型的实测修订，不代表全部语义能力已验证。
      tags:
      - 聊天(Chat)
      parameters:
      - name: Content-Type
        in: header
        description: ''
        required: true
        example: application/json
        schema:
          type: string
      - name: Accept
        in: header
        description: ''
        required: true
        example: application/json
        schema:
          type: string
      - name: Authorization
        in: header
        description: ''
        required: false
        example: Bearer {{YOUR_API_KEY}}
        schema:
          type: string
          default: Bearer {{YOUR_API_KEY}}
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                model:
                  type: string
                  description: 当前支持 gemini-3.1-flash-lite-image 或 gemini-nano-banana-2.1。
                  enum:
                  - gemini-3.1-flash-lite-image
                  - gemini-nano-banana-2.1
                messages:
                  type: array
                  items:
                    type: object
                    properties:
                      role:
                        type: string
                      content:
                        type: string
                    x-apifox-orders:
                    - role
                    - content
                  description: 至今为止对话所包含的消息列表。Python 代码示例。
                temperature:
                  type: number
                  description: 使用什么采样温度，介于 0 和 2 之间。较高的值（如 0.8）将使输出更加随机，而较低的值（如 0.2）将使输出更加集中和确定。  我们通常建议改变这个或`top_p`但不是两者。
                top_p:
                  type: number
                  description: 一种替代温度采样的方法，称为核采样，其中模型考虑具有 top_p 概率质量的标记的结果。所以 0.1 意味着只考虑构成前 10% 概率质量的标记。  我们通常建议改变这个或`temperature`但不是两者。
                n:
                  type: integer
                  description: '默认为 1

                    为每个输入消息生成多少个聊天补全选择。'
                stream:
                  type: boolean
                  description: '默认为 false 如果设置,则像在 ChatGPT 中一样会发送部分消息增量。标记将以仅数据的服务器发送事件的形式发送,这些事件在可用时,并在 data: [DONE]
                    消息终止流。Python 代码示例。'
                stop:
                  type: string
                  description: 默认为 null 最多 4 个序列,API 将停止进一步生成标记。
                max_tokens:
                  type: integer
                  description: '默认为 inf

                    在聊天补全中生成的最大标记数。


                    输入标记和生成标记的总长度受模型的上下文长度限制。计算标记的 Python 代码示例。'
                presence_penalty:
                  type: number
                  description: -2.0 和 2.0 之间的数字。正值会根据到目前为止是否出现在文本中来惩罚新标记，从而增加模型谈论新主题的可能性。  [查看有关频率和存在惩罚的更多信息。](https://platform.openai.com/docs/api-reference/parameter-details)
                frequency_penalty:
                  type: number
                  description: 默认为 0 -2.0 到 2.0 之间的数字。正值根据文本目前的存在频率惩罚新标记,降低模型重复相同行的可能性。  有关频率和存在惩罚的更多信息。
                logit_bias:
                  type: object
                  description: '修改指定标记出现在补全中的可能性。


                    接受一个 JSON 对象,该对象将标记(由标记器指定的标记 ID)映射到相关的偏差值(-100 到 100)。从数学上讲,偏差在对模型进行采样之前添加到模型生成的 logit 中。确切效果因模型而异,但-1
                    和 1 之间的值应减少或增加相关标记的选择可能性;如-100 或 100 这样的值应导致相关标记的禁用或独占选择。

                    实测传入对象与 null 均返回 HTTP 200；此结果仅证明请求被接受，未验证偏置对 Gemini token 的作用。'
                  nullable: true
                  additionalProperties:
                    type: number
                user:
                  type: string
                  description: 代表您的最终用户的唯一标识符，可以帮助 OpenAI 监控和检测滥用行为。[了解更多](https://platform.openai.com/docs/guides/safety-best-practices/end-user-ids)。
                response_format:
                  type: object
                  properties: {}
                  x-apifox-orders: []
                  description: '指定模型必须输出的格式的对象。  将 { "type": "json_object" } 启用 JSON 模式,这可以确保模型生成的消息是有效的 JSON。  重要提示:使用
                    JSON 模式时,还必须通过系统或用户消息指示模型生成 JSON。如果不这样做,模型可能会生成无休止的空白流,直到生成达到令牌限制,从而导致延迟增加和请求“卡住”的外观。另请注意,如果
                    finish_reason="length",则消息内容可能会被部分切断,这表示生成超过了 max_tokens 或对话超过了最大上下文长度。  显示属性'
                seen:
                  type: integer
                  description: 原始规范键名为 seen，描述称随机种子。本次 seen=123、seed=123 与任意未知字段均返回 HTTP 200，不能确认 seen 的语义或是否生效；保留原始键名，不推荐将其作为已验证的种子参数。
                tools:
                  type: array
                  items:
                    type: object
                    properties:
                      type:
                        type: string
                      function:
                        type: object
                        properties:
                          name:
                            type: string
                          description:
                            type: string
                          parameters:
                            type: object
                  description: '模型可以调用的一组工具列表。目前,只支持作为工具的函数。使用此功能来提供模型可以为之生成 JSON 输入的函数列表。

                    实测对象数组被接受，字符串数组返回 HTTP 500 invalid_request；强制 tool_choice 的成功响应未出现 tool_calls，不作为函数调用生效证明。'
                tool_choice:
                  type: object
                  properties: {}
                  description: '控制模型调用哪个函数(如果有的话)。none 表示模型不会调用函数,而是生成消息。auto 表示模型可以在生成消息和调用函数之间进行选择。通过 {"type":
                    "function", "function": {"name": "my_function"}} 强制模型调用该函数。  如果没有函数存在,默认为 none。如果有函数存在,默认为 auto。  显示可能的类型'
                  x-apifox-orders: []
              required:
              - model
              - messages
              x-apifox-orders:
              - model
              - messages
              - temperature
              - top_p
              - n
              - stream
              - stop
              - max_tokens
              - presence_penalty
              - frequency_penalty
              - logit_bias
              - user
              - response_format
              - seen
              - tools
              - tool_choice
            example:
              model: gemini-3.1-flash-lite-image
              messages:
              - role: user
                content: A cat.
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
                  object:
                    type: string
                  created:
                    type: integer
                  choices:
                    type: array
                    items:
                      type: object
                      properties:
                        index:
                          type: integer
                        message:
                          type: object
                          properties:
                            role:
                              type: string
                            content:
                              type: string
                          required:
                          - role
                          - content
                          x-apifox-orders:
                          - role
                          - content
                        finish_reason:
                          type: string
                      x-apifox-orders:
                      - index
                      - message
                      - finish_reason
                  usage:
                    type: object
                    properties:
                      prompt_tokens:
                        type: integer
                      completion_tokens:
                        type: integer
                      total_tokens:
                        type: integer
                    required:
                    - prompt_tokens
                    - completion_tokens
                    - total_tokens
                    x-apifox-orders:
                    - prompt_tokens
                    - completion_tokens
                    - total_tokens
                required:
                - id
                - object
                - created
                - choices
                - usage
                x-apifox-orders:
                - id
                - object
                - created
                - choices
                - usage
              example:
                id: chatcmpl-123
                object: chat.completion
                created: 1677652288
                choices:
                - index: 0
                  message:
                    role: assistant
                    content: '


                      Hello there, how may I assist you today?'
                  finish_reason: stop
                usage:
                  prompt_tokens: 9
                  completion_tokens: 12
                  total_tokens: 21
          headers: {}
          x-apifox-name: OK
      security: []
      x-apifox-folder: 聊天(Chat)
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-139393491-run
components:
  schemas: {}
  securitySchemes: {}
servers:
- url: https://ai.t8star.org
security: []
```

**支持范围：** `gemini-3.1-flash-lite-image`、`gemini-nano-banana-2.1`；`nano-banana-pro` 不支持此格式。

以下参数对照使用当前仍支持 Chat 的 `gemini-3.1-flash-lite-image`，因此保留相应类型修订：

| 原规范矛盾 | 实测观察 | 文档说明 |
| --- | --- | --- |
| temperature / top_p 声明 integer，但示例含小数 | 0.7 / 0.9 分别返回 200 | 使用 number；未验证采样效果 |
| logit_bias 声明 null，但描述为对象 | 对象与 null 均返回 200 | 使用可空 object；未验证偏置效果 |
| tools.items 声明 string | 字符串数组 500；对象数组 200 | 使用 object 数组；没有成功 tool_calls 证据 |
| seen 的键名与种子描述不一致 | seen、seed、任意未知字段均返回 200 | 不据此确认种子字段生效，保留原键名并注明未验证 |

证据：[实测记录 `chat-temperature`](validation/2026-10-20/chat-temperature.json)、[实测记录 `chat-top-p`](validation/2026-10-20/chat-top-p.json)、[实测记录 `chat-bias-object`](validation/2026-10-20/chat-bias-object.json)、[实测记录 `chat-bias-null`](validation/2026-10-20/chat-bias-null.json)、[实测记录 `chat-tools-string`](validation/2026-10-20/chat-tools-string.json)、[实测记录 `chat-tools-object`](validation/2026-10-20/chat-tools-object.json)、[实测记录 `chat-seen`](validation/2026-10-20/chat-seen.json)、[实测记录 `chat-seed`](validation/2026-10-20/chat-seed.json)、[实测记录 `chat-unknown`](validation/2026-10-20/chat-unknown.json)。

字符串 content 的文本与绘图请求已观察到成功；Chat 多模态输入数组、流式输出和 JSON 模式本次未验证。通用参数列表不代表所有语义功能均生效。

## Request Example

`{{MODEL}}` 选择 `gemini-3.1-flash-lite-image` 或 `gemini-nano-banana-2.1`。

Python(Requests):

```python
import requests
import json

url = "https://ai.t8star.org/v1/chat/completions"
payload = json.dumps({
   "model": "{{MODEL}}",
   "messages": [
      {
         "role": "user",
         "content": "A cat."
      }
   ]
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Accept": "application/json",
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

response = requests.request("POST", url, headers=headers, data=payload)
print(response.text)
```

Python(http.client):

```python
import http.client
import json

conn = http.client.HTTPSConnection("ai.t8star.org")
payload = json.dumps({
   "model": "{{MODEL}}",
   "messages": [
      {
         "role": "user",
         "content": "A cat."
      }
   ]
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Accept": "application/json",
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

conn.request("POST", "/v1/chat/completions", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
conn.close()
```

JavaScript:

```javascript
const myHeaders = new Headers();
myHeaders.append("Accept", "application/json");
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "model": "{{MODEL}}",
   "messages": [
      {
         "role": "user",
         "content": "A cat."
      }
   ]
});
const requestOptions = {
   method: "POST",
   headers: myHeaders,
   body: raw,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1/chat/completions", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

## Response Example

`gemini-3.1-flash-lite-image` 的真实绘图响应，HTTP 200，仅图片 URL 脱敏。图片链接位于 `choices[0].message.content` 的 Markdown 中，下载得到 JPEG 1408×768。证据：[实测记录 `chat-image`](validation/2026-10-20/chat-image.json)。

```json
{
  "id": "chatcmpl-EMaster202610200903471058543658268d9d6JbMMZmPc",
  "model": "gemini-3.1-flash-lite-image",
  "object": "chat.completion",
  "created": 1792487030,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "![image]([URL_OMITTED])"
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 19,
    "completion_tokens": 1120,
    "total_tokens": 1139,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "cache_write_tokens": 0,
      "text_tokens": 19,
      "audio_tokens": 0,
      "image_tokens": 0
    },
    "completion_tokens_details": {
      "text_tokens": 0,
      "audio_tokens": 0,
      "reasoning_tokens": 0
    },
    "input_tokens": 0,
    "output_tokens": 0,
    "input_tokens_details": null,
    "output_tokens_details": null,
    "claude_cache_creation_5_m_tokens": 0,
    "claude_cache_creation_1_h_tokens": 0
  },
  "system_fingerprint": null
}
```

`gemini-nano-banana-2.1` 的真实 Chat 响应如下，HTTP 200。该次提示要求回复 OK 文本，实际却返回了 Markdown 图片链接，下载后为 PNG 1024×1024；不能假定图像模型一定按文本请求返回纯文本。证据：[实测记录 `chat-2`](validation/2026-10-20/chat-2.json)。

```json
{
  "id": "chatcmpl-EMaster202610200903159447824108268d9d6X9vgF0tN",
  "model": "gemini-nano-banana-2.1",
  "object": "chat.completion",
  "created": 1792487021,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "![image]([URL_OMITTED])"
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 14,
    "completion_tokens": 617,
    "total_tokens": 631,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "cache_write_tokens": 0,
      "text_tokens": 14,
      "audio_tokens": 0,
      "image_tokens": 0
    },
    "completion_tokens_details": {
      "text_tokens": 0,
      "audio_tokens": 0,
      "reasoning_tokens": 0
    },
    "input_tokens": 0,
    "output_tokens": 0,
    "input_tokens_details": null,
    "output_tokens_details": null,
    "claude_cache_creation_5_m_tokens": 0,
    "claude_cache_creation_1_h_tokens": 0
  },
  "system_fingerprint": null
}
```

# OpenAI Dall-e Format

本格式按模型限制端点：`nano-banana-pro` 支持 Generations 与 Edits；`gemini-nano-banana-2.1` 仅支持 Generations；`gemini-3.1-flash-lite-image` 不支持本格式。

`nano-banana-pro` 返回图片 URL；官方返回空图导致的失败不扣费，按补充信息记录，未独立核对账单。原始文档中的旧模型别名、4K 描述及其他模型的历史探针不扩展为当前模型的能力或计费保证。

# Nano-banana(Generations)

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /v1/images/generations:
    post:
      summary: Nano-banana(Generations)
      deprecated: false
      description: OpenAI Dall-e Format 的 Generations。仅支持 nano-banana-pro、gemini-nano-banana-2.1；gemini-3.1-flash-lite-image
        不支持本格式。nano-banana-pro 返回图片 URL；按补充说明，官方返回空图导致的失败不扣费。
      tags:
      - 绘图模型/OpenAI Dall-e 格式
      parameters:
      - name: Authorization
        in: header
        description: ''
        required: false
        example: Bearer {{YOUR_API_KEY}}
        schema:
          type: string
          default: Bearer {{YOUR_API_KEY}}
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                model:
                  type: string
                  enum:
                  - nano-banana-pro
                  - gemini-nano-banana-2.1
                  example: nano-banana-pro
                prompt:
                  type: string
                  x-apifox-mock: 一只猫
                response_format:
                  type: string
                  description: 原始规范列出 url 或 b64_json；当前 nano-banana-pro 按补充说明返回 URL，建议使用 url。当前支持模型的 b64_json 行为未验证。
                  example: url
                aspect_ratio:
                  type: string
                  x-apifox-mock: '4:3'
                  enum:
                  - '4:3'
                  - '3:4'
                  - '16:9'
                  - '9:16'
                  - '2:3'
                  - '3:2'
                  - '1:1'
                  - '4:5'
                  - '5:4'
                  - '21:9'
                  x-apifox-enum:
                  - value: '4:3'
                    name: ''
                    description: ''
                  - value: '3:4'
                    name: ''
                    description: ''
                  - value: '16:9'
                    name: ''
                    description: ''
                  - value: '9:16'
                    name: ''
                    description: ''
                  - value: '2:3'
                    name: ''
                    description: ''
                  - value: '3:2'
                    name: ''
                    description: ''
                  - value: '1:1'
                    name: ''
                    description: ''
                  - value: '4:5'
                    name: ''
                    description: ''
                  - value: '5:4'
                    name: ''
                    description: ''
                  - value: '21:9'
                    name: ''
                    description: ''
                image:
                  type: array
                  items:
                    type: string
                  description: 原始规范描述为参考图字符串数组，支持 URL 或 b64_json。当前支持模型上的编码形式与图生图行为尚未验证。
              required:
              - model
              - prompt
              x-apifox-orders:
              - model
              - prompt
              - aspect_ratio
              - response_format
              - image
            example:
              model: nano-banana-pro
              prompt: A cat.
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
                x-apifox-orders: []
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 绘图模型/OpenAI Dall-e 格式
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-341817446-run
components:
  schemas: {}
  securitySchemes: {}
servers:
- url: https://ai.t8star.org
security: []
```

**支持范围：** `nano-banana-pro`、`gemini-nano-banana-2.1`；`gemini-3.1-flash-lite-image` 不支持。

两个受支持模型都已观察到 `data[].url` 返回。`nano-banana-pro` 的 URL 返回形式另由本次补充确认。原始规范的 aspect_ratio 枚举、image 字符串数组及 response_format 描述保留，但当前模型的各参数组合尚未逐项验证。此前对 `gemini-3.1-flash-lite-image` 做的 Data URL / 裸 Base64 / b64_json 测试，不能用来认定本节支持模型的参数行为。

## Request Example

`{{MODEL}}` 选择 `nano-banana-pro` 或 `gemini-nano-banana-2.1`；下方为无参考图最小请求。

Python(Requests):

```python
import requests
import json

url = "https://ai.t8star.org/v1/images/generations"
payload = json.dumps({
   "prompt": "A cat.",
   "model": "{{MODEL}}"
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

response = requests.request("POST", url, headers=headers, data=payload)
print(response.text)
```

Python(http.client):

```python
import http.client
import json

conn = http.client.HTTPSConnection("ai.t8star.org")
payload = json.dumps({
   "prompt": "A cat.",
   "model": "{{MODEL}}"
}, ensure_ascii=False).encode("utf-8")
headers = {
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "application/json"
}

conn.request("POST", "/v1/images/generations", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
conn.close()
```

JavaScript:

```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "prompt": "A cat.",
   "model": "{{MODEL}}"
});
const requestOptions = {
   method: "POST",
   headers: myHeaders,
   body: raw,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1/images/generations", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

## Response Example

`nano-banana-pro` 的真实 Generations 响应，HTTP 200，仅图片 URL 脱敏。该次请求只发送 model、prompt，没有发送 response_format；实际返回 URL，图片下载为 JPEG 1408×768。证据：[实测记录 `gen-1`](validation/2026-10-20/gen-1.json)。

```json
{
  "created": 1792486978,
  "data": [
    {
      "revised_prompt": "Create one simple image: a solid blue circle centered on a plain white background. No text.",
      "url": "[URL_OMITTED]"
    }
  ],
  "model": "nano-banana-pro",
  "usage": {
    "prompt_tokens": 33,
    "completion_tokens": 1270,
    "total_tokens": 1303,
    "input_tokens": 33,
    "output_tokens": 1270
  }
}
```

`gemini-nano-banana-2.1` 的 `A cat.` 请求返回 HTTP 200 与 URL，图片下载为 PNG 1024×1024。首次较长提示曾返回 451，不能保证所有提示均成功。证据：[实测记录 `followup-gen-2-simple`](validation/2026-10-20/followup-gen-2-simple.json)。

```json
{
  "created": 1792487708,
  "data": [
    {
      "revised_prompt": "A cat.",
      "url": "[URL_OMITTED]"
    }
  ],
  "model": "gemini-nano-banana-2.1",
  "usage": {
    "prompt_tokens": 17,
    "completion_tokens": 572,
    "total_tokens": 589,
    "input_tokens": 17,
    "output_tokens": 572
  }
}
```

# Nano-banana(Edits)

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /v1/images/edits:
    post:
      summary: Nano-banana(Edits)
      deprecated: false
      description: OpenAI Dall-e Format 的 Edits。仅支持 nano-banana-pro；gemini-nano-banana-2.1、gemini-3.1-flash-lite-image
        不支持本端点。nano-banana-pro 返回图片 URL；按补充说明，官方返回空图导致的失败不扣费。
      tags:
      - 绘图模型/OpenAI Dall-e 格式
      parameters:
      - name: Authorization
        in: header
        description: ''
        required: false
        example: Bearer {{YOUR_API_KEY}}
        schema:
          type: string
          default: Bearer {{YOUR_API_KEY}}
      requestBody:
        content:
          multipart/form-data:
            schema:
              type: object
              properties:
                model:
                  type: string
                  example: nano-banana-pro
                  enum:
                  - nano-banana-pro
                prompt:
                  type: string
                  example: 一只猫
                image:
                  type: array
                  items:
                    type: string
                    format: binary
                  description: 可省略；nano-banana-pro 无参考图请求实测成功。多图按原始规范重复 image 表单字段上传；该模型的历史单图请求返回 451，尚无参考图编辑成功样例。
                response_format:
                  type: string
                  description: 使用 url；nano-banana-pro 的图片结果按补充说明返回 URL。
                  enum:
                  - url
                  example: url
                aspect_ratio:
                  type: string
                  enum:
                  - '1:1'
                  - '2:3'
                  - '3:2'
                  - '3:4'
                  - '4:3'
                  - '4:5'
                  - '5:4'
                  - '9:16'
                  - '16:9'
                  - '21:9'
                  x-apifox-enum:
                  - value: '1:1'
                    name: ''
                    description: ''
                  - value: '2:3'
                    name: ''
                    description: ''
                  - value: '3:2'
                    name: ''
                    description: ''
                  - value: '3:4'
                    name: ''
                    description: ''
                  - value: '4:3'
                    name: ''
                    description: ''
                  - value: '4:5'
                    name: ''
                    description: ''
                  - value: '5:4'
                    name: ''
                    description: ''
                  - value: '9:16'
                    name: ''
                    description: ''
                  - value: '16:9'
                    name: ''
                    description: ''
                  - value: '21:9'
                    name: ''
                    description: ''
                  example: ''
              required:
              - model
              - prompt
              example:
                model: nano-banana-pro
                prompt: A cat.
                response_format: url
            example:
              model: nano-banana-pro
              prompt: A cat.
              response_format: url
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
                x-apifox-orders: []
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 绘图模型/OpenAI Dall-e 格式
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-341817449-run
components:
  schemas: {}
  securitySchemes: {}
servers:
- url: https://ai.t8star.org
security: []
```

**支持范围：仅 `nano-banana-pro`。** `gemini-nano-banana-2.1`、`gemini-3.1-flash-lite-image` 均不支持 Edits，即使历史探针曾取得 200，也不列为兼容模型。

补充信息确认 `nano-banana-pro` 支持 Edits；历史实测的无参考图请求成功，图片返回 URL。相同模型的蓝圆 PNG 单图请求（包括简化提示 `A red circle.`）仍返回 451，因此目前有接口支持说明和无参考图成功样例，尚无参考图编辑成功样例。证据：[实测记录 `edits-1`](validation/2026-10-20/edits-1.json)、[实测记录 `followup-edits-1-simple`](validation/2026-10-20/followup-edits-1-simple.json)、[实测记录 `followup-edits-1-no-reference`](validation/2026-10-20/followup-edits-1-no-reference.json)。

原始规范称支持多图或无参考图；本文保留可选 image 数组与重复 image 字段的请求形式。原始示例中的 size 未在属性表声明，之前对不受支持模型的尺寸实验不适用于 nano-banana-pro，因此当前 schema 不据此增加 size。aspect_ratio 与多图效果尚未在 nano-banana-pro 上验证成功。

## Request Example

以下多图请求形式依据原始规范整理，模型固定为 `nano-banana-pro`，当前未采集成功的多图编辑响应。替换 `{{PROMPT}}`、密钥和本地路径；Requests 与浏览器 FormData 会自动生成 multipart Content-Type 与 boundary。浏览器需选中两张图片，并取索引 0、1。

Python(Requests):

```python
import requests

url = "https://ai.t8star.org/v1/images/edits"
payload = {
   "model": "nano-banana-pro",
   "prompt": "{{PROMPT}}",
   "response_format": "url"
}
headers = {"Authorization": "Bearer {{YOUR_API_KEY}}"}

with open(r"E:\Downloads\reference-1.png", "rb") as image1, \
     open(r"E:\Downloads\reference-2.jpg", "rb") as image2:
   files = [
      ("image", ("reference-1.png", image1, "image/png")),
      ("image", ("reference-2.jpg", image2, "image/jpeg"))
   ]
   response = requests.request("POST", url, headers=headers, data=payload, files=files)
   print(response.text)
```

Python(http.client):

```python
import http.client
import mimetypes
import uuid
from pathlib import Path

conn = http.client.HTTPSConnection("ai.t8star.org")
boundary = "----t8star-" + uuid.uuid4().hex
data_list = []
fields = {
   "model": "nano-banana-pro",
   "prompt": "{{PROMPT}}",
   "response_format": "url"
}

for name, value in fields.items():
   data_list.extend([
      ("--" + boundary).encode("ascii"),
      ('Content-Disposition: form-data; name="%s"' % name).encode("ascii"),
      b"",
      value.encode("utf-8")
   ])

for file_path in [r"E:\Downloads\reference-1.png", r"E:\Downloads\reference-2.jpg"]:
   path = Path(file_path)
   file_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
   data_list.extend([
      ("--" + boundary).encode("ascii"),
      ('Content-Disposition: form-data; name="image"; filename="%s"' % path.name).encode("utf-8"),
      ("Content-Type: " + file_type).encode("ascii"),
      b"",
      path.read_bytes()
   ])

data_list.extend([("--" + boundary + "--").encode("ascii"), b""])
payload = b"\r\n".join(data_list)
headers = {
   "Authorization": "Bearer {{YOUR_API_KEY}}",
   "Content-Type": "multipart/form-data; boundary=" + boundary
}
conn.request("POST", "/v1/images/edits", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
conn.close()
```

JavaScript:

```javascript
// 浏览器示例：fileInput 为页面上的 <input type="file" multiple> 元素。
// 选择两张参考图后执行。
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");

const formdata = new FormData();
formdata.append("model", "nano-banana-pro");
formdata.append("prompt", "{{PROMPT}}");
formdata.append("image", fileInput.files[0], fileInput.files[0].name);
formdata.append("image", fileInput.files[1], fileInput.files[1].name);
formdata.append("response_format", "url");

const requestOptions = {
   method: "POST",
   headers: myHeaders,
   body: formdata,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1/images/edits", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

### 无参考图最小请求（已实测成功）

仅使用 `nano-banana-pro`。`files` 中的 `(None, value)` 强制生成与实测一致的 multipart 字段，不添加 image。

```python
import requests

response = requests.post(
   "https://ai.t8star.org/v1/images/edits",
   headers={"Authorization": "Bearer {{YOUR_API_KEY}}"},
   files={
      "model": (None, "nano-banana-pro"),
      "prompt": (None, "A cat."),
      "response_format": (None, "url")
   }
)
print(response.text)
```

## Response Example

`nano-banana-pro` 的无参考图真实 Edits 响应，HTTP 200，仅图片 URL 脱敏。该次发送 model、prompt=`A cat.`、response_format=`url`，未传 image。URL 图片实际下载为 JPEG 1408×768，并目视确认是一只猫；这不替代参考图编辑效果验证。证据：[实测记录 `followup-edits-1-no-reference`](validation/2026-10-20/followup-edits-1-no-reference.json)。

```json
{
  "created": 1792487714,
  "data": [
    {
      "revised_prompt": "A cat.",
      "url": "[URL_OMITTED]"
    }
  ],
  "model": "nano-banana-pro",
  "usage": {
    "prompt_tokens": 17,
    "completion_tokens": 1270,
    "total_tokens": 1287,
    "input_tokens": 17,
    "output_tokens": 1270
  }
}
```

蓝圆 PNG 参考图加 `A red circle.` 的真实错误如下，HTTP 451；本次未获得图片。保留该失败事实，与“模型支持此端点”的补充说明分别记录。证据：[实测记录 `followup-edits-1-simple`](validation/2026-10-20/followup-edits-1-simple.json)。

```json
{
  "error": {
    "message": "The request was rejected by the content safety policy. Please revise the prompt or reference images.",
    "type": "new_api_error",
    "param": "",
    "code": "451"
  }
}
```

`usage` 是服务端返回的记录，不代表独立核验的账单结果；“官方返回空图失败不扣费”按补充说明记录，当前 451 响应未被确认属于该空图场景。
