# 电商商品六视角图片 MCP Server

通过本地 MCP Server 准备商品 Form 和 Mock 任务，使用 Sunburst 或 Flare 生成单张六视角组合图，并查询、保存结果。Form 以商品形态为基础；Mock 在已审核 Form 上结合包装设计素材。请使用有权上传的图片，并人工核对结构和图案。

负责人已验收两种模型的 Form 和 Mock 真实结果。该验证采用特定网关、单次输入组合和人工操作；不代表所有输入数量、网关或使用环境均已验证。Team 模式下真实生成默认关闭，启用后提交可能产生费用；超时或结果未知时不要重新生成。

**当前工作区尚未形成包含上述功能的已发布版本。** GitHub 现有通过 Windows CI 的旧基线不包含当前完整功能，请勿把它当作此版安装包。待提交、CI 和发布完成后，按 [成员手册](MANUAL.md) 中的固定版本安装。

- 运行环境：Windows，Python 3.12.14，uv 0.12.9；依赖由 `uv.lock` 固定。
- 本地 stdio Host：Codex CLI、Claude Desktop、DeepSeek Harness Desktop（desktop Profile）；各自显示方式不同，见手册。
- 工具：`prepare_product_image`、`submit_product_image`、`get_product_image_task`、`get_product_image_file`、`review_form`、`confirm_mock_inputs`。
- Team 模式可记录成员的 Form 审核和 Mock 素材变更；实际 Host 联调和新入口的真实业务测试仍待验证。Strict 模式不启用这两个入口。

需求及任务状态见 [功能规范](specs/001-product-image-generation/spec.md)、[任务](specs/001-product-image-generation/tasks.md) 和 [工具契约](specs/001-product-image-generation/contracts/mcp-tools.md)。现有任务复选框仍是正式状态依据；真实图片验收不等于所有历史 Host、安全或 CI 验收项均已完成。
