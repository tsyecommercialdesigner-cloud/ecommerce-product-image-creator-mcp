# Gemini 网关资料：历史摘要

本文件自撰摘要基于本地留存的第三方网关资料与 2026-10-27 诊断记录，原始第三方文档未随仓库发布。它不是供应商的现行协议声明，也不证明项目当前已集成 LLM。模型可用性、端点、费用和实际兼容性需另行确认。

历史资料列出 `gemini-3.8-flash`、`gemini-3.7-flash`，并描述 Gemini 格式及 OpenAI Chat Completions 风格的图片分析接口；其图片输入示例使用文本与 `image_url`，但所附 schema 将 `content` 定义为字符串，二者存在差异。原资料中的接口与第三方网关行为不能直接等同于官方接口。

## 2026-10-27 单图兼容观察

一次经第三方转发的图片分析请求中，JSON Schema 的 `$defs`、`$ref` 被拒绝；后续复测还观察到嵌套 `additionalProperties`、`exclusiveMinimum` 兼容问题。最终测试使用引用内联和约束转换，并在客户端按原始模型校验；首张参考图的两类分析通过该校验。此为当时单图测试结果，不能证明批量、备用模型或其他网关的行为，也不能把兼容子集当成完整约束。原始资料未明确承诺 `response_format` 的全部功能。

通用接口参考：[Google 结构化输出](https://ai.google.dev/gemini-api/docs/structured-output)；[OpenAI Chat API](https://platform.openai.com/docs/api-reference/chat)。这些官方资料不能证明第三方网关兼容性。
