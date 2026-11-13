# integrations · 方案 C 集成测试床

阶段 3（M1）在这里搭：进程级拉起 advisor / bot / auth / stt-server / llm-gateway（假模型后端），
假飞书 IM 总线（模拟小A/小B 会话、事件重放）、假 OAuth（跳过浏览器直接发码）。

约定：只依赖六个模块的**公开包接口与书面契约**（不导入彼此源码）；跨模块链路用例放本目录，
模块内用例放各自 `tests/`。在仓库根目录运行：

```bash
python -m unittest discover -s integrations -v
```
