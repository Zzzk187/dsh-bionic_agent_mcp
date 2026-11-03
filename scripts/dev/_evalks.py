import os, re, sys, tempfile, io, json

os.environ['BIONIC_DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'agent.db')
os.environ.setdefault('FASTEMBED_CACHE_PATH', os.path.abspath('data/model_cache'))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import db, knowledge

CASES = [
    ('上下文坍缩', ['2026-10-20-长期Agent上下文与记忆工程.md']),
    ('交接单 handoff', ['2026-10-20-学习第二批-交接与语音.md']),
    ('切分 召回', ['2026-10-20-RAG切分与三层架构.md']),
    ('PagedAttention', ['2026-10-20-推理引擎与vLLM.md']),
    ('LoRA 参数', ['2026-10-20-参数高效微调新进展.md']),
    ('DPO PPO 奖励模型', ['2026-10-20-私有化部署与训练算法.md']),
    ('全参训练 成本', ['2026-10-20-私有化部署与训练算法.md']),
    ('CosyVoice 方言', ['2026-10-20-语音克隆与微调实践.md', '2026-10-20-本地TTS与声音克隆.md']),
    ('IndexTTS 情绪', ['2026-10-20-学习第二批-交接与语音.md', '2026-10-20-本地TTS与声音克隆.md']),
    ('缓存 前缀 命中率', ['2026-10-20-提示词缓存原理与省钱.md', '2026-10-20-AI成本核算与单位经济学.md']),
    ('Rubric 置信区间', ['2026-10-20-评测与成本-Benchmark与计费.md']),
    ('作弊 钩子 榜单', ['2026-10-20-Agent评测与技能包.md']),
    ('注入 外部数据 红线', ['2026-10-20-多Agent编排与知识图谱.md', '2026-10-20-学习50条.md', '2026-10-20-代刷B站Agent的设计与账号风控.md']),
    ('feature map 验证', ['2026-10-20-AI编码的信任与验证.md']),
    ('Harness 成功率 60 90', ['2026-10-20-Agent工程与上下文实战.md']),
    ('skill 审计 脚本', ['2026-10-20-Skill生态与Harness构建.md', '2026-10-20-AI编码的信任与验证.md']),
    ('视觉 RAG OCR', ['2026-10-20-RAG视觉与推理部署.md']),
    ('知识图谱 融合 靶点', ['2026-10-20-私有化部署与训练算法.md']),
    ('浏览器 自动化 token', ['2026-10-20-浏览器自动化与本地部署.md']),
    ('代码地图 根指针', ['2026-10-20-大型代码库的AI工程实践.md']),
    ('LangGraph 选型', ['2026-10-20-Agent框架选型地图.md']),
    ('人格 BFI 漂移', ['2026-10-20-Agent评测与技能包.md']),
    ('Obsidian Claudian', ['2026-10-20-AI知识库与Obsidian.md']),
    ('本地模型 V100 显存', ['2026-10-20-浏览器自动化与本地部署.md']),
    ('全参训练 87 万美元', ['2026-10-20-私有化部署与训练算法.md']),
    ('GLM-130B 硬件 100-150 万', ['2026-10-20-私有化部署与训练算法.md']),
    ('知识图谱 靶点 45 天', ['2026-10-20-私有化部署与训练算法.md']),
    ('Harness 消融 裸跑 9 美元', ['2026-10-20-Skill生态与Harness构建.md']),
    ('生成器 评估器 罚平庸', ['2026-10-20-Skill生态与Harness构建.md']),
    ('Voicebox 国区补丁', ['2026-10-20-语音克隆与微调实践.md']),
    ('DramaBox Gemma 12B 编码器', ['2026-10-20-语音克隆与微调实践.md']),
    ('LAYRA 整页图 视觉 RAG', ['2026-10-20-RAG视觉与推理部署.md']),
    ('AutoRound 逐层量化 600B', ['2026-10-20-RAG视觉与推理部署.md']),
    ('ASI-Bench 撤方法', ['2026-10-20-Agent评测与技能包.md']),
    ('SciΠvot 判断力 随机', ['2026-10-20-Agent评测与技能包.md']),
    ('Cursor 46.9% token 瘦身', ['2026-10-20-Agent工程与上下文实战.md']),
    ('ADK 编译视图', ['2026-10-20-Agent工程与上下文实战.md']),
    ('ReAct 5-7 次调用', ['2026-10-20-Agent工程与上下文实战.md']),
]


def main():
    db.init_db()
    knowledge.import_docs()
    r1 = r3 = 0
    misses = []
    for q, want in CASES:
        res = knowledge.search(q, limit=5, root='knowledge')
        files = []
        for x in res['results']:
            name = os.path.basename(x['source'].replace('\\', '/'))
            if name not in files:
                files.append(name)
        ok1 = bool(files) and files[0] in want
        ok3 = any(f in want for f in files)
        r1 += ok1
        r3 += ok3
        if not ok1:
            misses.append((q, files[0] if files else '(none)', want[0]))
    n = len(CASES)
    print(f'recall@1 {r1}/{n} = {100.0*r1/n:.0f}%')
    print(f'recall@3 {r3}/{n} = {100.0*r3/n:.0f}%')
    for m in misses:
        print(f'  MISS {m[0]} -> {m[1]}  (want {m[2]})')


main()
