# Models
- gpt-image-2.5-flare 支持 [gpt-image-2 Generations, gpt-image-2 Edits, OpenAI Dall-e Format, Asyncio Mode, Query Async Task]
- gpt-image-2.5-sunburst 支持 [gpt-image-2 Generations, gpt-image-2 Edits, OpenAI Dall-e Format, Asyncio Mode, Query Async Task]

# gpt-image-2 Generations

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ""
  description: ""
  version: 1.0.0
paths:
  /v1/images/generations:
    post:
      description: |-
        ### gpt-image-2 Generations
        逆向分组仅支持 model、prompt、size、image、quality 参数
        
        官转组 
        兼容edits 接口所有参数透传
        https://developers.openai.com/api/reference/resources/images/methods/edit
        
        size 介绍：
        https://developers.openai.com/api/docs/guides/image-generation#calculating-costs
        Size and quality options  尺寸和质量选项
        
        gpt-image-2 accepts any resolution in the size parameter when it satisfies the constraints below. Square images are typically fastest to generate.
        gpt-image-2 接受任何分辨率作为 size 参数，只要满足以下约束条件即可。通常情况下，正方形图像的生成速度最快。
        
        Popular sizes  常用尺码	
        1024x1024 (square)
        1024x1024 （正方形）
        
        1536x1024 (landscape)
        1536x1024 （横向）
        
        1024x1536 (portrait)
        1024x1536 （竖屏）
        
        2048x2048 (2K square)
        2048x2048 （2K 正方形）
        
        2048x1152 (2K landscape)
        2048x1152 （2K 横向）
        
        3840x2160 (4K landscape)
        3840x2160 （4K 横向）
        
        2160x3840 (4K portrait)
        2160x3840 （4K 竖屏）
        
        auto (default)
        auto （默认）
        
        Size constraints  尺寸限制	
        Maximum edge length must be less than or equal to 3840px
        最大边长必须小于或等于 3840px
        
        Both edges must be multiples of 16px
        两个边长都必须是 16px 的倍数。
        
        Long edge to short edge ratio must not exceed 3:1
        长边与短边之比不得超过 3:1
        
        Total pixels must be at least 655,360 and no more than 8,294,400
        总像素数必须至少为 655,360 ，且不得超过 8,294,400
        
        Quality options  优质选项	
        low  低的
        medium  中间
        high  高的
        auto (default)
        auto （默认）
      parameters: []
      requestBody:
        content:
          application/json:
            schema:
              $schema: "http://json-schema.org/draft-07/schema#"
              properties:
                image:
                  items:
                    type: string
                  type: array
                model:
                  type: string
                prompt:
                  type: string
                response_format:
                  description: "\"url\" or \"b64_json\". 默认 b64_json"
                  type: string
                size:
                  description: 建议使用 size，与官方一致
                  type: string
              required:
                - model
                - prompt
              title: gpt-image-2 Generations
              type: object
              x-apifox-id: "447261009"
        required: true
      responses:
        "200":
          content:
            application/json:
              schema:
                properties: {}
                type: object
          description: 成功
      summary: gpt-image-2 Generations
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []
```

## Request Example

Python(Requests):
```python
import requests
import json

url = "https://ai.t8star.org/v1/images/generations"

payload = json.dumps({
   "model": "{{MODEL}}",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}",
   "response_format": "b64_json",
   "image": [
      "string"
   ]
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}

response = requests.request("POST", url, headers=headers, data=payload)

print(response.text)
```

Python(http.client):
```python
import http.client
import json

conn = http.client.HTTPSConnection("")
payload = json.dumps({
   "model": "{{MODEL}}",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}",
   "response_format": "b64_json",
   "image": [
      "string"
   ]
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}
conn.request("POST", "https://ai.t8star.org/v1/images/generations", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
```

JavaScript:
```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "model": "{{MODEL}}",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}",
   "response_format": "b64_json",
   "image": [
      "string"
   ]
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

实测日期：2026-10-20（Asia/Shanghai）；本次 HTTP 状态码为 `200`。以下为真实 JSON 返回，仅将图片 URL 替换为 `[IMAGE_URL_OMITTED]`；其余字段和值保持原样。该占位符不可用于下载。

请求：`POST https://ai.t8star.org/v1/images/generations`；`model=gpt-image-2.5-flare`，`size=1024x1024`，提示词见返回的 `revised_prompt`。

本次请求仅发送 `model`、`prompt`、`size`，未发送 `response_format`、`quality`、`n` 或参考图。实际返回 `data[].url`，未返回 `b64_json`；不据此保证其他分组或参数组合的返回形式。

```json
{
  "created": 1792478578,
  "data": [
    {
      "revised_prompt": "A plain blue ceramic mug centered on a white background, simple studio photograph, no text.",
      "url": "[IMAGE_URL_OMITTED]"
    }
  ],
  "model": "gpt-image-2.5-flare",
  "usage": {
    "prompt_tokens": 18,
    "completion_tokens": 1650,
    "total_tokens": 1668,
    "input_tokens": 18,
    "output_tokens": 1650,
    "input_tokens_details": {
      "cached_tokens": 0,
      "text_tokens": 18,
      "audio_tokens": 0,
      "image_tokens": 0
    }
  }
}
```

`created` 为服务端返回的 Unix 秒时间戳；`usage` 为供应商返回的用量记录，不代表独立核验的计费结果。

# gpt-image-2 Edits

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
      summary: gpt-image-2 Edits
      deprecated: false
      description: "逆向分组仅支持 model、prompt、size、image、quality 参数\n\n官转组 \n兼容edits 接口所有参数透传\nhttps://developers.openai.com/api/reference/resources/images/methods/edit\n\nsize 介绍：\nhttps://developers.openai.com/api/docs/guides/image-generation#calculating-costs\nSize and quality options  尺寸和质量选项\n\ngpt-image-2 accepts any resolution in the size parameter when it satisfies the constraints below. Square images are typically fastest to generate.\ngpt-image-2 接受任何分辨率作为 size 参数，只要满足以下约束条件即可。通常情况下，正方形图像的生成速度最快。\n\nPopular sizes  常用尺码\t\n1024x1024 (square)\n1024x1024 （正方形）\n\n1536x1024 (landscape)\n1536x1024 （横向）\n\n1024x1536 (portrait)\n1024x1536 （竖屏）\n\n2048x2048 (2K square)\n2048x2048 （2K 正方形）\n\n2048x1152 (2K landscape)\n2048x1152 （2K 横向）\n\n3840x2160 (4K landscape)\n3840x2160 （4K 横向）\n\n2160x3840 (4K portrait)\n2160x3840 （4K 竖屏）\n\nauto (default)\nauto （默认）\n\nSize constraints  尺寸限制\t\nMaximum edge length must be less than or equal to 3840px\n最大边长必须小于或等于 3840px\n\nBoth edges must be multiples of 16px\n两个边长都必须是 16px 的倍数。\n\nLong edge to short edge ratio must not exceed 3:1\n长边与短边之比不得超过 3:1\n\nTotal pixels must be at least 655,360 and no more than 8,294,400\n总像素数必须至少为 655,360 ，且不得超过 8,294,400\n\nQuality options  优质选项\t\nlow  低的\nmedium  中间\nhigh  高的\nauto (default)\nauto （默认）\n"
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
                image:
                  description: 支持多图
                  example: E:\\Desktop\\gpt\\icon_samll2.png
                  type: string
                  format: binary
                prompt:
                  example: 带上眼镜
                  type: string
                model:
                  description: gpt-image-2
                  example: gpt-image-2
                  type: string
                size:
                  type: string
                  enum:
                    - 1024x1024
                    - 1536x1024
                    - 1024x1536
                  x-apifox-enum:
                    - value: 1024x1024
                      name: ''
                      description: ''
                    - value: 1536x1024
                      name: ''
                      description: ''
                    - value: 1024x1536
                      name: ''
                      description: ''
                  example: ''
                response_format:
                  description: '"url" or "b64_json". 默认 b64_json'
                  example: ''
                  type: string
              required:
                - image
                - prompt
                - model
            examples: {}
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
                x-apifox-orders: []
              example:
                created: 1713833628
                data:
                  - b64_json: ...
                usage:
                  total_tokens: 100
                  input_tokens: 50
                  output_tokens: 50
                  input_tokens_details:
                    text_tokens: 10
                    image_tokens: 40
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 绘图模型/OpenAI Dall-e 格式
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-447258891-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

## Request Example

Python(Requests):
```python
import requests

url = "https://ai.t8star.org/v1/images/edits"

payload = {}
files=[
   ('image',('E:\\Desktop\\gpt\\icon_samll2.png',open('E:\\\\Desktop\\\\gpt\\\\icon_samll2.png','rb'),'image/png'))
]
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}'
}

response = requests.request("POST", url, headers=headers, data=payload, files=files)

print(response.text)
```

Python(http.client):
```python
import http.client
import mimetypes
from codecs import encode

conn = http.client.HTTPSConnection("")
dataList = []
boundary = 'wL36Yn8afVp8Ag7AmP8qZ0SA4n1v9T'
dataList.append(encode('--' + boundary))
dataList.append(encode('Content-Disposition: form-data; name=image; filename={0}'.format('E:\\\\Desktop\\\\gpt\\\\icon_samll2.png')))

fileType = mimetypes.guess_type('E:\\\\Desktop\\\\gpt\\\\icon_samll2.png')[0] or 'application/octet-stream'
dataList.append(encode('Content-Type: {}'.format(fileType)))
dataList.append(encode(''))

with open('E:\\Desktop\\gpt\\icon_samll2.png', 'rb') as f:
   dataList.append(f.read())
dataList.append(encode('--' + boundary))
dataList.append(encode('Content-Disposition: form-data; name=prompt;'))

dataList.append(encode('Content-Type: {}'.format('text/plain')))
dataList.append(encode(''))

dataList.append(encode("{{PROMPT}}"))
dataList.append(encode('--' + boundary))
dataList.append(encode('Content-Disposition: form-data; name=model;'))

dataList.append(encode('Content-Type: {}'.format('text/plain')))
dataList.append(encode(''))

dataList.append(encode("{{MODEL}}"))
dataList.append(encode('--' + boundary))
dataList.append(encode('Content-Disposition: form-data; name=size;'))

dataList.append(encode('Content-Type: {}'.format('text/plain')))
dataList.append(encode(''))

dataList.append(encode(""))
dataList.append(encode('--' + boundary))
dataList.append(encode('Content-Disposition: form-data; name=response_format;'))

dataList.append(encode('Content-Type: {}'.format('text/plain')))
dataList.append(encode(''))

dataList.append(encode(""))
dataList.append(encode('--'+boundary+'--'))
dataList.append(encode(''))
body = b'\r\n'.join(dataList)
payload = body
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-type': 'multipart/form-data; boundary={}'.format(boundary)
}
conn.request("POST", "https://ai.t8star.org/v1/images/edits", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
```

JavaScripit:
```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");

const formdata = new FormData();
formdata.append("image", fileInput.files[0], "E:\\Desktop\\gpt\\icon_samll2.png");
formdata.append("prompt", "{{PROMPT}}");
formdata.append("model", "{{MODEL}}");
formdata.append("size", "{{SIZE}}");
formdata.append("response_format", "b64_json");

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

## Response Example

实测日期：2026-10-20（Asia/Shanghai）；本次 HTTP 状态码为 `200`。以下为真实 JSON 返回，仅将图片 URL 替换为 `[IMAGE_URL_OMITTED]`；其余字段和值保持原样。该占位符不可用于下载。

请求：`POST https://ai.t8star.org/v1/images/edits`；以 `multipart/form-data` 发送 `model=gpt-image-2.5-sunburst`、`size=1024x1024` 和提示词，使用 `image[]` 上传一张本地构造的 1024×1024 蓝色杯子 PNG 测试图。提示词见返回的 `revised_prompt`。未发送 `response_format`、`quality` 或 `n`。

```json
{
  "created": 1792478615,
  "data": [
    {
      "revised_prompt": "Change only the blue mug to red. Keep the composition and white background unchanged.",
      "url": "[IMAGE_URL_OMITTED]"
    }
  ],
  "model": "gpt-image-2.5-sunburst",
  "usage": {
    "prompt_tokens": 17,
    "completion_tokens": 1056,
    "total_tokens": 1073,
    "input_tokens": 17,
    "output_tokens": 1056,
    "input_tokens_details": {
      "cached_tokens": 0,
      "text_tokens": 16,
      "audio_tokens": 0,
      "image_tokens": 1
    }
  }
}
```

本次实际返回图片 URL 和上述 `usage` 字段；上方 OpenAPI 中的 Base64/100 tokens 示例为原始文档示意值，不是本次实测。API 返回成功不等同于已经验证图片编辑质量。

# OpenAI Dall-e Format

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
      summary: Flux
      deprecated: false
      description: |+
        model 支持
        flux
        flux-dev
        flux-pro

        size 支持 
        1024x512
        1024x1024
        1024x576
        1024x768
        1024x512
        512x1024
        512x768
        1280x960
        1280x960
        960x1280
        960x1280
        768x1366
        768x512
        1366x768
        1344x576


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
              properties: {}
              x-apifox-orders: []
            example:
              model: flux-dev
              prompt: cat
              size: 1024x1024
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
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-139454705-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

## Request Example

Python(Requests):
```python
import requests
import json

url = "https://ai.t8star.org/v1/images/generations"

payload = json.dumps({
   "model": "flux-dev",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}"
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}

response = requests.request("POST", url, headers=headers, data=payload)

print(response.text)
```

Python(http.client):
```python
import http.client
import json

conn = http.client.HTTPSConnection("")
payload = json.dumps({
   "model": "flux-dev",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}"
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}
conn.request("POST", "https://ai.t8star.org/v1/images/generations", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
```

JavaScript:
```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "model": "flux-dev",
   "prompt": "{{PROMPT}}",
   "size": "{{SIZE}}"
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

本节复用上方 Generations 的同一次真实请求与响应：两节使用相同的 `POST /v1/images/generations` 端点，实测模型为 `gpt-image-2.5-flare`，`size=1024x1024`。没有额外发起 Flux 请求，因此本示例不证明 `flux`、`flux-dev` 或 `flux-pro` 的权限、参数兼容性或响应结构。

实测日期：2026-10-20（Asia/Shanghai）；本次 HTTP 状态码为 `200`。以下为真实 JSON 返回，仅将图片 URL 替换为 `[IMAGE_URL_OMITTED]`；其余字段和值保持原样。该占位符不可用于下载。

```json
{
  "created": 1792478578,
  "data": [
    {
      "revised_prompt": "A plain blue ceramic mug centered on a white background, simple studio photograph, no text.",
      "url": "[IMAGE_URL_OMITTED]"
    }
  ],
  "model": "gpt-image-2.5-flare",
  "usage": {
    "prompt_tokens": 18,
    "completion_tokens": 1650,
    "total_tokens": 1668,
    "input_tokens": 18,
    "output_tokens": 1650,
    "input_tokens_details": {
      "cached_tokens": 0,
      "text_tokens": 18,
      "audio_tokens": 0,
      "image_tokens": 0
    }
  }
}
```

# Asyncio Mode

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
      summary: '所有画图(异步) '
      deprecated: false
      description: |-
        支持所有画图模型异步请求，仅需添加 query 参数 async=true，请求body 与之前一致即可；

        提交图像生成请求：
        使用 POST /v1/images/generations 提交图像生成请求并传递参数（如 size、prompt、model 等）。
        响应中返回一个 task_id，此 task_id 用于后续查询图像生成状态。

        查询图像生成结果：
        使用 GET /v1/images/tasks/{task_id} 查询生成任务的状态。如果任务完成，响应会包含生成的图像 URL。
      tags:
        - 绘图模型/OpenAI Dall-e 格式
      parameters:
        - name: async
          in: query
          description: 启用异步模式
          required: true
          example: 'true'
          schema:
            type: string
        - name: webhook
          in: query
          description: ''
          required: false
          example: https://www.baidu.com/notify
          schema:
            type: string
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
                prompt:
                  type: string
                model:
                  type: string
              required:
                - prompt
                - model
              x-apifox-orders:
                - prompt
                - model
            example:
              size: 1024x1024
              prompt: cat
              model: sora_image
              'n': 1
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties:
                  code:
                    type: string
                  message:
                    type: string
                  data:
                    type: string
                    description: task id
                required:
                  - code
                  - message
                  - data
                x-apifox-orders:
                  - code
                  - message
                  - data
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 绘图模型/OpenAI Dall-e 格式
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-356258955-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

## Request Example

Python(Requests):
```python
import requests
import json

url = "https://ai.t8star.org/v1/images/generations?async=true"

payload = json.dumps({
   "size": "{{SIZE}}",
   "prompt": "{{PROMPT}}",
   "model": "{{MODEL}}",
   "n": 1
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}

response = requests.request("POST", url, headers=headers, data=payload)

print(response.text)
```

Python(http.client):
```python
import http.client
import json

conn = http.client.HTTPSConnection("")
payload = json.dumps({
   "size": "{{SIZE}}",
   "prompt": "{{PROMPT}}",
   "model": "{{MODEL}}",
   "n": 1
})
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}
conn.request("POST", "https://ai.t8star.org/v1/images/generations?async=true", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
```

JavaScript:
```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const raw = JSON.stringify({
   "size": "{{SIZE}}",
   "prompt": "{{PROMPT}}",
   "model": "{{MODEL}}",
   "n": 1
});

const requestOptions = {
   method: "POST",
   headers: myHeaders,
   body: raw,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1/images/generations?async=true", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

## Response Example

实测日期：2026-10-20（Asia/Shanghai）；请求 `POST https://ai.t8star.org/v1/images/generations?async=true`，HTTP `200`。请求 JSON 如下；未发送 `n`、`response_format` 或 `webhook`：

```json
{
  "model": "gpt-image-2.5-flare",
  "prompt": "A plain blue ceramic mug centered on a white background, simple studio photograph, no text.",
  "size": "1024x1024"
}
```

真实提交响应如下，未做字段改写：

```json
{
  "task_id": "4ba39518ad05461db91b51e76d893b19"
}
```

**实测差异：** 本次返回顶层 `task_id`，未返回上方 OpenAPI 声明的 `code`、`message`、`data` 包装；不能将该 schema 当作所有分组的唯一响应形式。提交成功只表示已获得任务 ID，不表示图片已生成。使用该 ID 查询下节端点；收费 POST 不自动重试，超时或断连时不能据此判断提交失败。

# Query Async Task

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /v1/images/tasks/{task_id}:
    get:
      summary: '所有画图(查询异步结果) '
      deprecated: false
      description: |-
        status： 
        IN_PROGRESS （处理中）
        FAILURE （失败）
        SUCCESS （成功）
      tags:
        - 绘图模型/OpenAI Dall-e 格式
      parameters:
        - name: task_id
          in: path
          description: ''
          required: true
          example: ''
          schema:
            type: string
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
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
              example:
                code: success
                message: ''
                data:
                  task_id: 3dad96708a77485e97ac7ef652796d7b
                  platform: sync-task
                  action: image-sync
                  status: SUCCESS
                  fail_reason: ''
                  submit_time: 1758993864
                  start_time: 1758993885
                  finish_time: 1758993885
                  progress: 100%
                  data:
                    data:
                      - url: >-
                          https://files.closeai.fans/filesystem/output/20250927/f870fae3-b0c3-4e4d-ba13-387a6556c613.png
                        b64_json: ''
                        revised_prompt: 一只小猫在炒菜
                    model: nano-banana
                    created: 1758993885
                    usage:
                      prompt_tokens: 1425
                      completion_tokens: 1290
                      total_tokens: 2715
                      prompt_tokens_details:
                        cached_tokens: 0
                        text_tokens: 0
                        audio_tokens: 0
                        image_tokens: 0
                      completion_tokens_details:
                        text_tokens: 0
                        audio_tokens: 0
                        reasoning_tokens: 0
                      input_tokens: 0
                      output_tokens: 0
                      input_tokens_details: null
                      output_tokens_details: null
                  search_item: ''
          headers: {}
          x-apifox-name: 成功
      security: []
      x-apifox-folder: 绘图模型/OpenAI Dall-e 格式
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/3868318/apis/api-356258956-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

## Request Example

Python(Requests):
```python
import requests
import json

url = "https://ai.t8star.org/v1/images/tasks/{{TASK_ID}}"

payload = {}
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
```

Python(http.client):
```python
import http.client
import json

conn = http.client.HTTPSConnection("")
payload = ''
headers = {
   'Authorization': 'Bearer {{YOUR_API_KEY}}',
   'Content-Type': 'application/json'
}
conn.request("GET", "https://ai.t8star.org/v1/images/tasks/{{TASK_ID}}", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
```

JavaScript:
```javascript
const myHeaders = new Headers();
myHeaders.append("Authorization", "Bearer {{YOUR_API_KEY}}");
myHeaders.append("Content-Type", "application/json");

const requestOptions = {
   method: "GET",
   headers: myHeaders,
   redirect: "follow"
};

fetch("https://ai.t8star.org/v1/images/tasks/{{TASK_ID}}", requestOptions)
   .then((response) => response.text())
   .then((result) => console.log(result))
   .catch((error) => console.error(error));
```

## Response Example

实测日期：2026-10-20（Asia/Shanghai）；请求 `GET https://ai.t8star.org/v1/images/tasks/4ba39518ad05461db91b51e76d893b19`。以下两次只读查询均返回 HTTP `200`，关联上节同一个任务。

### 等待执行

```json
{
  "code": "success",
  "message": "",
  "data": {
    "task_id": "4ba39518ad05461db91b51e76d893b19",
    "platform": "sync-task",
    "action": "image-sync",
    "status": "NOT_START",
    "fail_reason": "",
    "submit_time": 1792478535,
    "start_time": 0,
    "finish_time": 0,
    "progress": "0%",
    "data": null,
    "cost": 0,
    "search_item": ""
  }
}
```

**实测补充：** 除上方列出的状态外，本次还观察到 `NOT_START`；此时 `progress` 为 `"0%"`，`data` 为 `null`，开始和完成时间为 `0`。应保留任务 ID 继续查询，不重新提交。

### 执行成功

实测日期：2026-10-20（Asia/Shanghai）；本次 HTTP 状态码为 `200`。以下为真实 JSON 返回，仅将图片 URL 替换为 `[IMAGE_URL_OMITTED]`；其余字段和值保持原样。该占位符不可用于下载。

```json
{
  "code": "success",
  "message": "",
  "data": {
    "task_id": "4ba39518ad05461db91b51e76d893b19",
    "platform": "sync-task",
    "action": "image-sync",
    "status": "SUCCESS",
    "fail_reason": "",
    "submit_time": 1792478535,
    "start_time": 1792478606,
    "finish_time": 1792478606,
    "progress": "100%",
    "data": {
      "data": [
        {
          "url": "[IMAGE_URL_OMITTED]",
          "b64_json": "",
          "revised_prompt": "A plain blue ceramic mug centered on a white background, simple studio photograph, no text."
        }
      ],
      "model": "gpt-image-2.5-flare",
      "created": 1792478606,
      "usage": {
        "prompt_tokens": 18,
        "completion_tokens": 1650,
        "total_tokens": 1668,
        "prompt_tokens_details": {
          "cached_tokens": 0,
          "cache_write_tokens": 0,
          "text_tokens": 0,
          "audio_tokens": 0,
          "image_tokens": 0
        },
        "completion_tokens_details": {
          "text_tokens": 0,
          "audio_tokens": 0,
          "reasoning_tokens": 0
        },
        "input_tokens": 18,
        "output_tokens": 1650,
        "input_tokens_details": {
          "cached_tokens": 0,
          "cache_write_tokens": 0,
          "text_tokens": 18,
          "audio_tokens": 0,
          "image_tokens": 0
        },
        "output_tokens_details": null,
        "claude_cache_creation_5_m_tokens": 0,
        "claude_cache_creation_1_h_tokens": 0
      }
    },
    "cost": 0.15,
    "search_item": ""
  }
}
```

完成状态位于外层 `data.status`，图片列表位于 `data.data.data`，即首张图片 URL 为 `data.data.data[0].url`。本次 `b64_json` 是空字符串，不能作为图片数据解码。`cost` 为供应商原样返回的数值，响应未注明币种或单位，不将其解释为固定价格。`FAILURE` 与 `IN_PROGRESS` 的真实响应本次未采集，不构造实测示例。

