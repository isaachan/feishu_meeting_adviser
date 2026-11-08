# 集成测试

本目录默认全部为 integration 测试：模块隔离守护、本地会话 CLI 流程，以及 fake-recorder → 本地交接契约 → advisor 的端到端链路。模块单元测试分别位于 `advisor/tests/`、`meeting-listener/tests/` 和 `local-meeting-recorder/tests/`。

使用独立的临时测试环境，从仓库根目录执行：

```bash
python3 -m venv /tmp/meeting-integration
/tmp/meeting-integration/bin/python -m pip install -e ./advisor -e ./meeting-listener -e ./local-meeting-recorder
/tmp/meeting-integration/bin/python -m unittest discover -s integrations -v
```

CI 分别在只安装单个模块的环境中运行模块测试，再在安装全部模块的环境中运行本目录测试。仓库根目录不维护共享 `pyproject.toml` 或共享业务代码。测试只使用临时配置、合成数据及假模型，不访问真实会议、本机 `data/` 或声卡。
