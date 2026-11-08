# 集成测试

本目录默认全部为 integration 测试，验证模块隔离、独立配置、入会介绍以及字幕采集、消费确认和数据删除的交接。模块单元测试分别位于 `advisor/tests/` 和 `meeting-listener/tests/`。

使用独立的临时测试环境，从仓库根目录执行：

```bash
python3 -m venv /tmp/meeting-integration
/tmp/meeting-integration/bin/python -m pip install -e ./advisor -e ./meeting-listener
/tmp/meeting-integration/bin/python -m unittest discover -s integrations -v
```

CI 分别在只安装单个模块的环境中运行模块测试，再在安装两个模块的环境中运行本目录测试。仓库根目录不维护共享 `pyproject.toml` 或共享业务代码。测试只使用临时配置、合成数据及假客户端，不访问真实会议或本机 `data/`。
