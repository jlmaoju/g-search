# 功能回归验证

在项目根目录、已安装项目 Python 运行依赖的环境中运行。服务测试额外使用 `httpx`，前端逻辑测试使用 Node.js 内置测试工具，无需 npm 安装。

```powershell
python -X utf8 -B -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/frontend_search.test.mjs
```

测试使用临时 SQLite 数据库、固定检索样例和模拟网络响应，不调用付费模型，不修改运行中的数据库或日更状态。

浏览器人工回归可启动以下独立服务，访问 http://127.0.0.1:8878/。页面直接使用当前项目的前端文件，接口返回测试数据。

```powershell
python -X utf8 -B tests/frontend_functional_fixture.py --port 8878
```

- 连续提交“慢查询验证”和“最新查询验证”，最后应显示后者。
- “断线恢复”第一次返回 503，再次搜索应恢复；换一个以“断线恢复”开头的查询可重新测试。
- “额度测试”显示已有的额度告警；“空结果”返回空列表。
- 在参与者中搜索并选择“测试参与者645”，检查筛选、焦点保留和 Escape 返回。
- 打开设置，用 Tab / Shift+Tab 检查焦点循环，Escape 后回到入口；搜索范围和筛选分组支持方向键。

固定查询样例用于防止已知问题复发，不代表全库相关性评测。
