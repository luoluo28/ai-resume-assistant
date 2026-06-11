# ai-resume-assistant
AI-powered resume analyzer for job applications
# AI Resume Assistant - AI简历助手
基于 Dify 搭建的简历解析、智能分析工作流，支持文本合并、简历信息提取。

## 项目介绍
针对简历 + 岗位描述文本进行整合解析，自动化提取简历关键信息，适用于求职简历优化、岗位匹配场景。

## 文件说明
- `workflow.yml`：Dify 工作流 DSL 配置文件（核心文件）

## 如何导入使用
1. 打开 Dify 平台，新建 **工作流应用**
2. 进入应用「编排」页面，点击右上角 `···`
3. 选择 **导入 DSL**，上传本仓库内的 `workflow.yml`
4. 导入完成后，**重新配置模型密钥、LLM 节点**（密钥不会随 DSL 导出）
5. 保存并发布应用，即可正常运行

## 技术栈
- Dify 低代码工作流
- Python 代码节点
- 大语言模型
