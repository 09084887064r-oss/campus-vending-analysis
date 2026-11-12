# 校园售货机销售提升分析 · 安装与使用说明

这个技能让 WorkBuddy 会做一件事：**看你的售货机销售数据，找出销售提升点**，产出
一份 Excel（十几个工作表）、一份单文件网页报告、一份 Word 报告，并按周/按月归档，
下期自动做环比。

装一次就行，以后每次把数据拖进对话说"帮我分析"即可。

---

## 一、装之前：确认这三件事

### 1. WorkBuddy 已装好并登录

### 2. 电脑上有 Python（本技能的执行环境）

技能本身是 Python 脚本，需要 **Python 3.9 或更高**（开发与实测环境是 3.14）。

在 Windows 上检查：按 `Win + R`，输入 `cmd` 回车，在黑窗口里打：

```
python --version
```

看到 `Python 3.9.x` 或更高就对了；提示"不是内部或外部命令"就是没装，去
<https://www.python.org/downloads/> 装一个（安装时勾上 **Add python.exe to PATH**）。

### 3. 依赖库（技能会自己告诉你缺什么）

| 库 | 用途 | 是否必需 |
| --- | --- | --- |
| `openpyxl` | 读写 Excel、导出 Excel/HTML | **必需** |
| `python-docx` | 导出 Word 报告 | 可选 |
| `matplotlib` | Word 报告里的图表 | 可选 |

**不需要你提前折腾**。缺库时技能会返回这样一段（不是英文报错）：

```json
{
  "status": "MISSING_DEPENDENCIES",
  "message": "运行本技能缺少依赖：openpyxl>=3.1。装好之后重跑即可……",
  "install_command": "C:\\Python314\\python.exe -m pip install openpyxl",
  "agent_instruction": "请先执行 install_command 安装依赖，然后重新运行同一条命令……"
}
```

把 `install_command` 那一行交给 WorkBuddy 执行（或自己粘到 cmd 里回车），装完重跑即可。
也可以一次性装全：

```
python -m pip install -r skills\campus-vending-analysis\scripts\requirements.txt
```

> 只缺 `python-docx` / `matplotlib` 时**不会中断**：Excel 和网页报告照常出，Word 跳过并说明原因。

---

## 二、安装技能（两种方式，任选其一）

### 方式 A：界面里上传（推荐）

1. 打开 WorkBuddy，看左侧竖排图标，找到 **「专家·技能·连接器」**，点它。
2. 点上方 **「技能」** → 右上角 **「添加技能」** → **「上传技能」**。
3. 选择 `campus-vending-analysis.zip`（**不用解压**），点打开。
4. 等几秒，出现 **"安装成功"**，列表里多出 **「校园售货机销售提升分析」**。

成功的三个标志：① 有安装成功提示 ② 列表里有这个名字 ③ 旁边开关是打开的。

> 这个 zip 里除了技能文件夹，还放了一份本说明（`INSTALL.md`）。
> 如果上传时提示"包结构不正确"，直接用方式 B——解压后只拖那个文件夹。

### 方式 B：手动复制（方式 A 失败时用）

1. 右键 `campus-vending-analysis.zip` → **全部解压**。
2. 解压出来的目录里**直接**有一个 `campus-vending-analysis` 文件夹
   （点进去能看到 `SKILL.md`、`scripts`、`assets`、`references`）。
3. 在资源管理器地址栏输入下面这行回车：
   ```
   %USERPROFILE%\.workbuddy\skills\
   ```
4. 把 `campus-vending-analysis` 文件夹**整个拖进去**。
   ⚠️ 拖完必须是 `skills\campus-vending-analysis\SKILL.md`，
   **不能**变成 `skills\campus-vending-analysis\campus-vending-analysis\SKILL.md`（多套一层就不认）。
5. **完全退出** WorkBuddy：右下角托盘图标右键 → 退出（不是关窗口）。
6. 重开 WorkBuddy → 「专家·技能·连接器」→「技能」→ 找到它 → **开关拨到开启**。

---

## 三、第一次用：准备三份数据

技能至少要一份销售明细；另外两份缺了会**停下来问你**（不会瞎猜）。

| # | 数据 | 必需 | 关键列（列名可映射，不用完全一样） |
| --- | --- | --- | --- |
| 1 | 销售明细 `csv / xlsx / json / tsv` | **必需** | 交易时间、机器编号、商品名称、销售数量、销售金额（用户标识、订单标识有则更准） |
| 2 | 商品分类表 | 做品类分析必需 | 商品名称、品类 |
| 3 | 楼栋人数表（新老生） | 做用户分析必需 | 楼栋、性别、老生人数、新生人数 |
| 4 | 机器→楼栋对照表 | 做楼栋分析必需 | 机器编号、楼栋 |

缺列、列名对不上、同一商品挂了两个品类、人数表少楼栋……它都会先列出来问你怎么处理，
**确认后才开始分析**。所以第一次大概率会是"它先问你几个问题"，这是正常的。

---

## 四、跑一轮

1. **新开一个对话**（旧对话不会加载新装的技能）。
2. 把销售明细拖进输入框，说一句：**帮我分析这份售货机数据**（要周报就说"按周"，要月报说"按月"）。
3. 它会依次问你：列名对不对 → 分类表/人数表在哪 → 按周还是按月 → 数据归档目录放哪。
4. 确认后开始跑，最后给你：Excel + 网页报告 + Word，以及一份**销售提升点清单**
   （每条含：问题 / 数据证据 / 影响范围 / 建议动作 / 责任对象 / 预期效果 / 优先级 / 验证方式）。

---

## 五、产出在哪

第一次会问你一个**数据根目录**（默认在"文档"下的"售货机数据归档"）。之后：

```
<数据根目录>/
  raw/2026-W45/        原始数据副本（不改你的源文件）
  processed/2026-W45/  清洗后明细 + 口径指纹（下期环比就靠它）
  reports/2026-W45/    Excel / HTML / Word 三份报告
```

- 周报周期键按 ISO 周（如 `2026-W45`），月报按自然月（如 `2026-11`）。
- 下一期跑的时候自动读上一期做环比；**同一期重复跑会先停下来问你**，确认后旧文件另存为 `.bak`，不覆盖。
- 没有上期数据时，报表里上期列显示 `N/A`，不会写成 0。

---

## 六、装完怎么验证

1. 新开对话，拖一份明细，说"帮我分析这份售货机数据"。
2. 如果它开始问问题（列名对不对、分类表在哪、按周还是按月），说明技能已生效。
3. 想更彻底地验证：让 WorkBuddy 执行
   ```
   python "%USERPROFILE%\.workbuddy\skills\campus-vending-analysis\scripts\run.py" selftest
   ```
   看到 `"passed": 85, "failed": 0` 就是环境+代码都正常。

---

## 七、没反应怎么办（按顺序试）

1. **看层级**：`%USERPROFILE%\.workbuddy\skills\campus-vending-analysis\SKILL.md` 能否打开？多套一层文件夹最常见。
2. **看开关**：「技能」→「已安装」里那个开关是不是关着。
3. **看 Python**：cmd 里 `python --version` 有没有反应；没装就装 Python。
4. **看依赖**：让 WorkBuddy 执行 `run.py deps`；返回 `MISSING_DEPENDENCIES` 就按里面的 `install_command` 装。
5. **重启 + 新开会话**：退出托盘图标重新打开，并开新对话。
6. 还不行就把「技能」页面截图 + `run.py deps` 的输出发给维护者。

---

## 八、升级与卸载

- **升级**：先卸载，再按上面重新装新 zip。
- **卸载**：「技能」页面把开关关掉，然后到 `%USERPROFILE%\.workbuddy\skills\` 删掉
  `campus-vending-analysis` 文件夹。归档数据在数据根目录里，卸载不会删。
