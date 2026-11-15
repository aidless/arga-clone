# arga-clone 的日常入口。所有目标都是"一条命令跑完、失败即退出"。
.PHONY: help test verify serve demo suite bench eval clean

help:
	@echo "make test    跑全套 pytest（410）"
	@echo "make verify  十层验收（场景/pytest/判分/HTTP/gold/覆盖/MCP/SDK/边界/真模型）"
	@echo "make serve   起服务，127.0.0.1:8787"
	@echo "make demo    三态判分演示"
	@echo "make suite   按 suite_*.py 重新生成 39 个 scenario JSON"
	@echo "make bench  真模型冒烟（5 题，需本地 LLM 网关）"

test:
	python3 -m pytest

verify:
	bash scripts/verify.sh

serve:
	python3 -m argaclone.cli serve --port 8787

demo:
	python3 demo/run_demo.py

suite:
	python3 scenarios/build_suite.py

bench:
	python3 demo/agent_compare.py --limit 1 --workers 5

# 兼容旧入口名：外部流程（EVAL_ACCEPTANCE_STANDARD P4）按 make eval 调
eval: test

clean:
	rm -rf .pytest_cache __pycache__ */__pycache__ */*/__pycache__
