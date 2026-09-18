"""R51：从教材总论抽取（C/D 两类）与人工整理（A/B 两类）医理规则，
写 `data/standard/tcm_theory.jsonl`。

## 为什么 A/B 两类是 curated 不是 classic

《中医基础理论》教材原文不在本项目里（授权 clone `PanckooAI/TCM_Datasets` 未
完成）。藏象关系与病机传变按藏象学说、病机学说的通用表述人工整理，`span`
是这条规则自身的标准表述，**不冒充某一版教材的逐字引用**——这是诚实标注，
不是降级：内容本身是学科公认的基础理论，只是这个项目没有一份可核对页码的
电子版本。将来拿到教材全文，这两类可以整批换成 `classic` 并补 `span`/`source`，
`core/theory.py` 的查询接口不用改。

## 为什么 C 类的 12 条经典治则也是 curated

同样的道理：`books/` 目录里没有《素问》或《中医基础理论》的电子版，"虚则补之"
这类治则口诀虽然是学科公认的标准提法，但本项目里找不到一份可以做子串校验的
原文。C 类里 nature/location 具体化的操作性治则也是同一情况。

## 为什么 D 类是 classic

`books/中药学.md`、`books/方剂学.md` 两本教材总论**确实在本项目里**，七情、
君臣佐使、组方原则、药味加减、药对经验全部来自这两本书的总论段落。`span`
在写文件之前会逐条断言是对应书文件内容的子串——抽不到就让脚本崩，不静默
放行一条编造的出处。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "standard" / "tcm_theory.jsonl"
BOOKS_DIR = ROOT / "books"

Confidence = Literal["classic", "derived", "curated"]

#: 这一版只在脾胃门范围内整理（跟 CLAUDE.md「项目脾胃门定位」一致），
#: 其余门类留给后续版本按同一形状补。
APPLIES_TO_ORGAN = "脾胃门"
APPLIES_TO_COMPAT = "组方通则"


# 藏象关系（A 类）。confidence="curated"：《中医基础理论》教材原文不在本项目里
# （授权 clone 未完成），下面是按藏象学说通用表述人工整理的条目，不是某一版
# 教材的逐字引用——`source` 字段如实说明这一点，不冒充抽取自具体页码。
ORGAN_RELATIONS = [
    # ---- 五行相生（正常生理） ----
    dict(subject="肝", relation="生", object="心", mechanism="木生火：肝藏血、心主血脉，肝血充足则心血得养",
         direction="木生火", trigger_elements=["肝", "血虚"], implied_elements=["心", "血虚"]),
    dict(subject="心", relation="生", object="脾", mechanism="火生土：心阳温煦脾土，助脾运化",
         direction="火生土", trigger_elements=["心", "阳虚"], implied_elements=["脾", "阳虚"]),
    dict(subject="脾", relation="生", object="肺", mechanism="土生金：脾运化水谷精微上输于肺，为气血生化之源",
         direction="土生金", trigger_elements=["脾", "气虚"], implied_elements=["肺", "气虚"]),
    dict(subject="肺", relation="生", object="肾", mechanism="金生水：肺主肃降通调水道，下输膀胱，肺气充足助肾藏精",
         direction="金生水", trigger_elements=["肺", "气虚"], implied_elements=["肾", "气虚"]),
    dict(subject="肾", relation="生", object="肝", mechanism="水生木：肾藏精，肝藏血，精血同源，肾精充足则肝血得养",
         direction="水生木", trigger_elements=["肾", "阴虚"], implied_elements=["肝", "阴虚"]),
    # ---- 五行相克（正常制约） ----
    dict(subject="肝", relation="克", object="脾", mechanism="木克土：肝之疏泄正常制约脾之壅滞，维持运化",
         direction="木克土", trigger_elements=["肝"], implied_elements=["脾"]),
    dict(subject="脾", relation="克", object="肾", mechanism="土克水：脾运化水湿以制约肾水泛溢",
         direction="土克水", trigger_elements=["脾"], implied_elements=["肾"]),
    dict(subject="肾", relation="克", object="心", mechanism="水克火：肾水上济以制约心火过亢",
         direction="水克火", trigger_elements=["肾"], implied_elements=["心"]),
    dict(subject="心", relation="克", object="肺", mechanism="火克金：心火下降以制约肺气过于清肃",
         direction="火克金", trigger_elements=["心"], implied_elements=["肺"]),
    dict(subject="肺", relation="克", object="肝", mechanism="金克木：肺气肃降以制约肝气升发太过",
         direction="金克木", trigger_elements=["肺"], implied_elements=["肝"]),
    # ---- 相乘（病理，过度克制） ----
    dict(subject="肝", relation="乘", object="脾", mechanism="肝气郁结或肝气亢逆，横逆犯脾，脾失健运",
         direction="木乘土", trigger_elements=["肝", "气滞"], implied_elements=["脾", "湿"]),
    dict(subject="肝", relation="乘", object="胃", mechanism="肝气郁结，横逆犯胃，胃失和降",
         direction="木乘土", trigger_elements=["肝", "气滞"], implied_elements=["胃", "气滞"]),
    dict(subject="脾", relation="乘", object="肾", mechanism="脾阳久虚，土不制水，累及肾阳",
         direction="土乘水", trigger_elements=["脾", "阳虚"], implied_elements=["肾", "阳虚"]),
    dict(subject="心", relation="乘", object="肺", mechanism="心火亢盛，上炎刑金，灼伤肺阴",
         direction="火乘金", trigger_elements=["心", "热"], implied_elements=["肺", "阴虚"]),
    dict(subject="肺", relation="乘", object="肝", mechanism="肺失肃降，燥金伐木太过，肝失疏泄",
         direction="金乘木", trigger_elements=["肺", "热"], implied_elements=["肝", "气滞"]),
    # ---- 相侮（病理，反向克制） ----
    dict(subject="脾", relation="侮", object="肝", mechanism="脾湿壅盛，反侮肝木，肝失疏泄",
         direction="土侮木", trigger_elements=["脾", "湿"], implied_elements=["肝", "气滞"]),
    dict(subject="肺", relation="侮", object="心", mechanism="肺气壅塞，反侮心火，心脉不畅",
         direction="金侮火", trigger_elements=["肺", "痰"], implied_elements=["心", "血瘀"]),
    dict(subject="肾", relation="侮", object="脾", mechanism="肾水泛滥，反侮脾土，脾阳受困",
         direction="水侮土", trigger_elements=["肾", "阳虚"], implied_elements=["脾", "阳虚"]),
    dict(subject="肝", relation="侮", object="肺", mechanism="肝火亢盛，上逆侮肺，肺失清肃",
         direction="木侮金", trigger_elements=["肝", "热"], implied_elements=["肺", "热"]),
    dict(subject="心", relation="侮", object="肾", mechanism="心火独亢，下侮肾水，水火不济",
         direction="火侮水", trigger_elements=["心", "热"], implied_elements=["肾", "阴虚"]),
    # ---- 表里（脏腑相合） ----
    dict(subject="脾", relation="表里", object="胃", mechanism="脾与胃同居中焦，以膜相连，脾病常累及胃，胃病常累及脾",
         direction="表里", trigger_elements=["脾"], implied_elements=["胃"]),
    dict(subject="胃", relation="表里", object="脾", mechanism="胃病日久，运化失司，可反过来影响脾之升清",
         direction="表里", trigger_elements=["胃"], implied_elements=["脾"]),
    dict(subject="肝", relation="表里", object="胆", mechanism="肝主疏泄，胆贮藏排泄胆汁，肝失疏泄常累及胆",
         direction="表里", trigger_elements=["肝"], implied_elements=["胆"]),
    dict(subject="胆", relation="表里", object="肝", mechanism="胆气郁滞，亦可影响肝之疏泄",
         direction="表里", trigger_elements=["胆"], implied_elements=["肝"]),
    dict(subject="心", relation="表里", object="小肠", mechanism="心与小肠相为表里，心火可下移小肠",
         direction="表里", trigger_elements=["心", "热"], implied_elements=["小肠", "热"]),
    dict(subject="小肠", relation="表里", object="心", mechanism="小肠实热亦可上扰心神",
         direction="表里", trigger_elements=["小肠", "热"], implied_elements=["心"]),
    dict(subject="肺", relation="表里", object="大肠", mechanism="肺与大肠相为表里，肺失肃降常致大肠传导失常",
         direction="表里", trigger_elements=["肺"], implied_elements=["大肠"]),
    dict(subject="大肠", relation="表里", object="肺", mechanism="大肠腑气不通，亦可影响肺气肃降",
         direction="表里", trigger_elements=["大肠"], implied_elements=["肺"]),
    # ---- 气血津液与脏腑功能关系 ----
    dict(subject="脾", relation="生化", object="心", mechanism="脾为气血生化之源，脾虚生化不足，心失所养",
         direction="母病及子", trigger_elements=["脾", "气虚"], implied_elements=["心", "血虚"]),
    dict(subject="肝", relation="藏血", object="心", mechanism="肝藏血以济心血，肝血不足则心血亦虚",
         direction="子盗母气", trigger_elements=["肝", "血虚"], implied_elements=["心", "血虚"]),
    dict(subject="肾", relation="藏精", object="肝", mechanism="肾藏精，肝藏血，精血同源，肾精不足则肝血失充",
         direction="精血同源", trigger_elements=["肾", "阴虚"], implied_elements=["肝", "阴虚"]),
    dict(subject="肺", relation="主气", object="心", mechanism="肺主气，心主血，气为血之帅，肺气虚则运血无力",
         direction="气为血帅", trigger_elements=["肺", "气虚"], implied_elements=["心", "血瘀"]),
    dict(subject="脾", relation="统血", object="心", mechanism="脾气统摄血液，脾气虚则血不循经，可致心血不足",
         direction="脾不统血", trigger_elements=["脾", "气虚"], implied_elements=["心", "血虚"]),
    dict(subject="肝", relation="疏泄", object="脾", mechanism="肝主疏泄以助脾胃升降运化，疏泄正常则运化有序",
         direction="木疏土", trigger_elements=["肝"], implied_elements=["脾"]),
    dict(subject="肝", relation="疏泄", object="胃", mechanism="肝主疏泄以助胃气通降，疏泄失常则胃失和降",
         direction="木疏土", trigger_elements=["肝"], implied_elements=["胃"]),
    dict(subject="心", relation="藏神", object="肾", mechanism="心肾相交，心火下降以温肾水，肾水上济以制心火",
         direction="心肾相交", trigger_elements=["心", "阴虚"], implied_elements=["肾", "阴虚"]),
    dict(subject="肾", relation="藏精", object="心", mechanism="肾水不足，不能上济心火，心火独亢",
         direction="心肾不交", trigger_elements=["肾", "阴虚"], implied_elements=["心", "热"]),
    dict(subject="肺", relation="治节", object="肾", mechanism="肺主通调水道，下输于肾，肺肾金水相生",
         direction="金水相生", trigger_elements=["肺", "阴虚"], implied_elements=["肾", "阴虚"]),
    dict(subject="脾", relation="散精", object="肺", mechanism="脾为生痰之源，肺为贮痰之器，脾失健运则痰湿上贮于肺",
         direction="脾病及肺", trigger_elements=["脾", "痰"], implied_elements=["肺", "痰"]),
    dict(subject="脾", relation="生化", object="肾", mechanism="脾为后天之本，肾为先天之本，后天不足可累及先天",
         direction="后天累先天", trigger_elements=["脾", "气虚"], implied_elements=["肾", "阳虚"]),
    dict(subject="肾", relation="温煦", object="脾", mechanism="肾阳为一身阳气之根本，肾阳不足则脾阳失于温煦",
         direction="命门火衰", trigger_elements=["肾", "阳虚"], implied_elements=["脾", "阳虚"]),
    dict(subject="胆", relation="主决断", object="脾", mechanism="胆气郁滞影响情志，情志不畅又可累及脾胃升降",
         direction="胆病及脾", trigger_elements=["胆", "气滞"], implied_elements=["脾", "气滞"]),
    dict(subject="三焦", relation="通调", object="脾", mechanism="三焦为水液与元气运行之道路，通调失常则脾之运化水湿受阻",
         direction="三焦病及脾", trigger_elements=["三焦", "湿"], implied_elements=["脾", "湿"]),
]
assert len(ORGAN_RELATIONS) >= 40, len(ORGAN_RELATIONS)
# 病机传变（B 类）。confidence="curated"，理由同 organ_relations.py。
PATHOMECHANISMS = [
    dict(from_=["气滞"], to=["热"], condition="气郁日久不解",
         mechanism="气滞则运行不畅，郁久化热", markers=["口苦", "烦躁", "舌红"]),
    dict(from_=["气滞"], to=["血瘀"], condition="气滞日久不解",
         mechanism="气为血帅，气行则血行，气滞则血行不畅而致瘀", markers=["刺痛", "舌质紫黯", "舌下络脉迂曲"]),
    dict(from_=["气虚"], to=["湿"], condition="脾气虚不能运化水湿",
         mechanism="脾气虚运化无力，水湿内停", markers=["肢体困重", "苔腻", "便溏"]),
    dict(from_=["气虚"], to=["血虚"], condition="气虚日久",
         mechanism="气能生血，气虚则化生血液的功能减退", markers=["面色淡白", "舌淡", "脉细弱"]),
    dict(from_=["气虚"], to=["阳虚"], condition="气虚进一步发展",
         mechanism="气虚日久，阳气化生不足，由气虚发展为阳虚", markers=["畏寒", "肢冷", "脉沉迟"]),
    dict(from_=["阳虚"], to=["寒"], condition="阳虚失于温煦",
         mechanism="阳气不足，温煦无权，内生虚寒", markers=["畏寒肢冷", "喜温喜按", "舌淡"]),
    dict(from_=["阳虚"], to=["水停"], condition="阳虚气化无力",
         mechanism="阳虚不能温化水液，水湿停聚，甚则泛溢肌肤", markers=["肢体浮肿", "小便不利", "舌淡胖"]),
    dict(from_=["阳虚"], to=["阴虚"], condition="阳虚日久，阳损及阴",
         mechanism="阳气虚衰日久，化生阴液的功能亦减退，阴阳两虚", markers=["五心烦热与畏寒并见", "舌淡少津"]),
    dict(from_=["阴虚"], to=["热"], condition="阴液不足，不能制阳",
         mechanism="阴虚不能制阳，虚阳偏亢而生内热", markers=["五心烦热", "潮热盗汗", "舌红少苔"]),
    dict(from_=["阴虚"], to=["津伤"], condition="阴虚进一步耗损",
         mechanism="阴液不足日久，津液化生亦不足", markers=["口干欲饮", "皮肤干燥", "舌红少津"]),
    dict(from_=["阴虚"], to=["阳虚"], condition="阴虚日久，阴损及阳",
         mechanism="阴液亏虚日久，无以化生阳气，阴阳两虚", markers=["潮热与畏寒并见", "舌淡红少苔"]),
    dict(from_=["血虚"], to=["阴虚"], condition="血虚日久",
         mechanism="血为阴液的重要组成部分，血虚日久可发展为阴虚", markers=["五心烦热", "舌红少苔", "脉细数"]),
    dict(from_=["血虚"], to=["血瘀"], condition="血虚运行无力",
         mechanism="血虚则脉道不充，运行无力，久而成瘀", markers=["面色晦暗", "舌质淡紫"]),
    dict(from_=["湿"], to=["热"], condition="湿邪郁遏日久",
         mechanism="湿性黏滞，郁遏气机，久蕴化热，形成湿热", markers=["苔黄腻", "身热不扬", "小便黄赤"]),
    dict(from_=["湿"], to=["痰"], condition="湿邪停聚不化",
         mechanism="湿聚为水，水停为饮，饮凝为痰，湿痰同源", markers=["苔白腻", "胸闷", "脉滑"]),
    dict(from_=["湿"], to=["气滞"], condition="湿邪阻遏气机",
         mechanism="湿性重浊黏滞，阻滞气机升降", markers=["脘腹痞满", "肢体困重"]),
    dict(from_=["痰"], to=["热"], condition="痰邪郁久",
         mechanism="痰邪停滞，郁遏化热，形成痰热", markers=["苔黄腻", "咳痰黄稠", "脉滑数"]),
    dict(from_=["痰"], to=["气滞"], condition="痰邪阻滞气机",
         mechanism="痰邪停聚，阻碍气机升降出入", markers=["胸闷", "喉中痰鸣"]),
    dict(from_=["食积"], to=["热"], condition="食积日久不消",
         mechanism="食积内停，郁久化热", markers=["口气臭秽", "苔黄厚腻", "大便臭秽"]),
    dict(from_=["食积"], to=["湿"], condition="食积影响运化",
         mechanism="食积阻滞，脾失健运，水湿内生", markers=["脘腹胀满", "苔厚腻"]),
    dict(from_=["血瘀"], to=["热"], condition="瘀血内阻日久",
         mechanism="瘀血阻滞，气机郁遏，久而化热", markers=["局部灼痛", "舌质紫黯有瘀斑", "低热"]),
    dict(from_=["血瘀"], to=["水停"], condition="瘀血阻滞脉络",
         mechanism="血不利则为水，瘀血阻滞脉络，津液渗于脉外", markers=["肢体肿胀", "舌质紫黯"]),
    dict(from_=["寒"], to=["血瘀"], condition="寒邪凝滞血脉",
         mechanism="寒性凝滞收引，血得寒则凝，凝而成瘀", markers=["冷痛拒按", "舌质紫黯", "遇寒加重"]),
    dict(from_=["寒"], to=["气滞"], condition="寒邪阻滞气机",
         mechanism="寒主收引，阻遏气机运行", markers=["拘急冷痛", "脉弦紧"]),
    dict(from_=["热"], to=["津伤"], condition="热邪炽盛",
         mechanism="热盛迫津外泄或灼伤津液", markers=["口渴引饮", "舌红少津", "小便短赤"]),
    dict(from_=["热"], to=["阴虚"], condition="热邪久羁",
         mechanism="邪热久留，耗伤阴液，由实转虚", markers=["低热不退", "舌红少苔", "脉细数"]),
    dict(from_=["热"], to=["血瘀"], condition="热邪灼伤血络",
         mechanism="热盛迫血妄行，或热灼营血黏滞成瘀", markers=["斑疹紫黯", "舌绛有瘀点"]),
    dict(from_=["久病"], to=["血瘀"], condition="久病不愈，入络成瘀",
         mechanism="久病气血运行不畅，由气及血，由经入络", markers=["痛处固定", "舌质紫黯", "病程日久"]),
    dict(from_=["久病"], to=["气虚"], condition="久病耗伤正气",
         mechanism="病程迁延，正气渐耗，由实转虚", markers=["神疲乏力", "食欲减退"]),
    # ---- 脾胃门常见的具体传变（组合证素） ----
    dict(from_=["脾", "气虚"], to=["脾", "湿"], condition="脾气虚不能运化水湿",
         mechanism="脾为生痰之源，脾气虚运化失职，水湿内停", markers=["食少便溏", "苔白腻", "肢体困重"]),
    dict(from_=["脾", "湿"], to=["脾", "阳虚"], condition="湿邪困阻脾阳日久",
         mechanism="湿为阴邪，最易损伤阳气，湿困日久损及脾阳", markers=["畏寒", "便溏", "舌淡胖苔白腻"]),
    dict(from_=["脾", "湿"], to=["脾", "热"], condition="湿邪郁遏化热",
         mechanism="湿热蕴结中焦，脾胃升降失常", markers=["苔黄腻", "身热不扬", "脘腹痞闷"]),
    dict(from_=["肝", "气滞"], to=["脾", "气滞"], condition="肝气郁结，横逆犯脾",
         mechanism="肝木乘土，肝气郁结影响脾之运化升清", markers=["胁肋胀痛", "腹胀", "便溏不爽"]),
    dict(from_=["肝", "气滞"], to=["胃", "气滞"], condition="肝气郁结，横逆犯胃",
         mechanism="肝木乘土，肝气郁结影响胃之通降", markers=["脘胁胀痛", "嗳气", "泛酸"]),
    dict(from_=["肝", "气滞"], to=["肝", "热"], condition="肝气郁结日久化火",
         mechanism="气有余便是火，肝郁日久化火", markers=["口苦", "目赤", "急躁易怒"]),
    dict(from_=["肝", "热"], to=["胃", "热"], condition="肝火横逆犯胃",
         mechanism="肝火亢盛，横逆犯胃，胃失和降", markers=["吞酸嘈杂", "口苦", "呕恶"]),
    dict(from_=["胃", "热"], to=["胃", "阴虚"], condition="胃热日久耗伤胃阴",
         mechanism="邪热久羁，灼伤胃阴", markers=["胃脘灼痛", "饥不欲食", "舌红少津"]),
    dict(from_=["脾", "阳虚"], to=["肾", "阳虚"], condition="脾阳虚衰日久，损及肾阳",
         mechanism="脾阳根于肾阳，脾阳久虚可损及肾阳，火不暖土", markers=["五更泄泻", "腰膝冷痛", "舌淡胖"]),
    dict(from_=["肾", "阳虚"], to=["脾", "阳虚"], condition="肾阳虚衰，火不暖土",
         mechanism="命门火衰，不能温煦脾阳，运化失职", markers=["五更泄泻", "食少便溏", "畏寒肢冷"]),
    dict(from_=["脾", "气虚"], to=["心", "血虚"], condition="脾虚日久，气血生化不足",
         mechanism="脾为气血生化之源，脾虚化源不足，心失所养", markers=["心悸", "失眠", "面色萎黄"]),
    dict(from_=["心", "血虚"], to=["心", "阴虚"], condition="心血虚日久",
         mechanism="血虚日久，阴液亦亏，心阴不足", markers=["心悸", "五心烦热", "舌红少苔"]),
    dict(from_=["肺", "气虚"], to=["肺", "阴虚"], condition="肺气虚日久，气阴两伤",
         mechanism="肺为娇脏，气虚日久，津液化生不足，累及肺阴", markers=["干咳少痰", "咽干", "舌红少苔"]),
    dict(from_=["肺", "阴虚"], to=["肺", "热"], condition="肺阴不足，虚火内生",
         mechanism="阴虚不能制阳，虚火上炎灼肺", markers=["咳嗽咽干", "潮热盗汗", "舌红少苔"]),
    dict(from_=["肾", "阴虚"], to=["肝", "阴虚"], condition="肾阴不足，精不化血",
         mechanism="肾水不能涵养肝木，精血同源，肾阴亏虚累及肝阴", markers=["头晕目眩", "腰膝酸软", "舌红少苔"]),
    dict(from_=["肝", "阴虚"], to=["肝", "热"], condition="肝阴不足，阴不制阳",
         mechanism="肝阴亏虚，肝阳偏亢，虚风内动", markers=["头晕目眩", "急躁易怒", "舌红少苔"]),
    dict(from_=["胆", "湿"], to=["胆", "热"], condition="湿邪郁阻胆腑化热",
         mechanism="湿热蕴结肝胆，胆气不利", markers=["口苦", "胁痛", "苔黄腻"]),
    dict(from_=["心", "痰"], to=["心", "热"], condition="痰浊郁阻心脉化热",
         mechanism="痰郁化火，扰乱心神", markers=["心烦不寐", "苔黄腻", "脉滑数"]),
    dict(from_=["肺", "痰"], to=["肺", "热"], condition="痰浊壅肺郁久化热",
         mechanism="痰浊内蕴，郁久化热，痰热壅肺", markers=["咳痰黄稠", "苔黄腻", "脉滑数"]),
    dict(from_=["脾", "食积"], to=["胃", "气滞"], condition="食积中焦，胃气壅滞",
         mechanism="食积不消，阻滞胃气通降", markers=["脘腹胀满", "嗳腐吞酸"]),
    dict(from_=["三焦", "湿"], to=["脾", "湿"], condition="三焦水道不利，湿聚中焦",
         mechanism="三焦为水液运行之道路，通调失职，湿邪停聚中焦困脾", markers=["脘腹痞闷", "肢体困重", "苔腻"]),
]
assert len(PATHOMECHANISMS) >= 50, len(PATHOMECHANISMS)
# 治则推导（C 类）。前 12 条是《素问》以来的经典治则（メタ规则，when_nature/
# when_location 留空表示"普遍适用"，principle 本身就是判据）；其余按
# nature（+可选 location）建立可被 principles_for() 直接匹配的操作性治则。
# confidence="classic"（源自历代医家共识的治则理论，非逐字页码引用时也标"classic"，
# 因为这些提法本身就是教材反复重申的标准表述——跟 curated 的区别在于它不是
# 本项目自己整理的，是学科公认的口诀）。
TREATMENT_PRINCIPLES = [
    # ---- 12 条经典治则（普遍适用，when_nature/when_location 空表示不限） ----
    dict(when_nature=[], when_location=[], principle="虚则补之",
         method_keywords=["补"], contraindicated_methods=["攻伐", "峻下"],
         span_note="正气不足者，以补虚扶正为治疗大法"),
    dict(when_nature=[], when_location=[], principle="实则泻之",
         method_keywords=["泻", "攻"], contraindicated_methods=["峻补"],
         span_note="邪气盛实者，以祛邪泻实为治疗大法"),
    dict(when_nature=["寒"], when_location=[], principle="寒者热之",
         method_keywords=["温", "热"], contraindicated_methods=["寒凉"],
         span_note="寒证以温热药治之"),
    dict(when_nature=["热"], when_location=[], principle="热者寒之",
         method_keywords=["清", "寒凉"], contraindicated_methods=["温热", "辛燥"],
         span_note="热证以寒凉药治之"),
    dict(when_nature=["气滞"], when_location=["肝"], principle="木郁达之",
         method_keywords=["疏肝", "理气", "解郁"], contraindicated_methods=["峻下", "破血"],
         span_note="肝气郁结者，宜疏达调畅"),
    dict(when_nature=["热"], when_location=[], principle="火郁发之",
         method_keywords=["透发", "清透"], contraindicated_methods=["寒凉冰伏"],
         span_note="热邪郁伏于内者，宜因势透发，不可单纯寒凉冰伏"),
    dict(when_nature=[], when_location=[], principle="塞因塞用",
         method_keywords=["补虚"], contraindicated_methods=["消导", "攻下"],
         span_note="因虚而致闭塞不通之证，以补开塞，反用补法治疗闭塞的假象"),
    dict(when_nature=[], when_location=[], principle="通因通用",
         method_keywords=["攻下", "通利"], contraindicated_methods=["固涩", "止泻"],
         span_note="因实邪内阻而致通泻之证，以通治通，反用通利法治疗通泻的假象"),
    dict(when_nature=[], when_location=[], principle="急则治标",
         method_keywords=["治标", "救急"], contraindicated_methods=["缓补"],
         span_note="病情危急时先治标以缓解急症，再图治本"),
    dict(when_nature=[], when_location=[], principle="缓则治本",
         method_keywords=["治本"], contraindicated_methods=[],
         span_note="病情平稳时针对根本病因病机施治"),
    dict(when_nature=[], when_location=[], principle="正治反治",
         method_keywords=["正治", "反治"], contraindicated_methods=[],
         span_note="正治是逆其证候性质而治，反治是顺从疾病假象而治"),
    dict(when_nature=[], when_location=[], principle="三因制宜",
         method_keywords=["因人", "因时", "因地"], contraindicated_methods=[],
         span_note="治疗须因人、因时、因地制宜，不可一方通治"),
    # ---- 按证素（nature）的操作性治则 ----
    dict(when_nature=["气虚"], when_location=[], principle="补气",
         method_keywords=["补气", "益气"], contraindicated_methods=["破气", "耗气"],
         span_note="气虚者以甘温之品益气扶正"),
    dict(when_nature=["阳虚"], when_location=[], principle="温阳",
         method_keywords=["温阳", "扶阳"], contraindicated_methods=["苦寒", "滋腻"],
         span_note="阳虚者以甘温之品温补阳气"),
    dict(when_nature=["阴虚"], when_location=[], principle="滋阴",
         method_keywords=["滋阴", "养阴"], contraindicated_methods=["辛燥", "温热"],
         span_note="阴虚者以甘寒咸寒之品滋养阴液"),
    dict(when_nature=["血虚"], when_location=[], principle="补血",
         method_keywords=["补血", "养血"], contraindicated_methods=["破血", "行血过度"],
         span_note="血虚者以甘温或甘平之品补养阴血"),
    dict(when_nature=["气滞"], when_location=[], principle="行气",
         method_keywords=["行气", "理气"], contraindicated_methods=["峻补壅滞"],
         span_note="气滞者以辛散之品行气解郁"),
    dict(when_nature=["血瘀"], when_location=[], principle="活血化瘀",
         method_keywords=["活血", "化瘀"], contraindicated_methods=["峻补收涩"],
         span_note="血瘀者以辛温或辛平之品活血通络"),
    dict(when_nature=["湿"], when_location=[], principle="化湿利湿",
         method_keywords=["化湿", "利湿", "燥湿"], contraindicated_methods=["滋腻", "峻补"],
         span_note="湿邪者以芳香或淡渗之品化湿利湿"),
    dict(when_nature=["痰"], when_location=[], principle="化痰祛痰",
         method_keywords=["化痰", "祛痰"], contraindicated_methods=["滋腻"],
         span_note="痰邪者以燥湿或清热之品化痰祛痰"),
    dict(when_nature=["食积"], when_location=[], principle="消食导滞",
         method_keywords=["消食", "导滞"], contraindicated_methods=["峻补"],
         span_note="食积者以消导之品消食化积"),
    dict(when_nature=["津伤"], when_location=[], principle="生津",
         method_keywords=["生津", "养阴"], contraindicated_methods=["辛燥", "利水伤津"],
         span_note="津伤者以甘凉甘寒之品生津润燥"),
    dict(when_nature=["饮"], when_location=[], principle="温化痰饮",
         method_keywords=["温化", "化饮"], contraindicated_methods=["滋腻助饮"],
         span_note="痰饮者以温药和之，通阳化饮"),
    # ---- 脏腑 + 证素的具体化治则 ----
    dict(when_nature=["湿"], when_location=["脾"], principle="健脾化湿",
         method_keywords=["健脾", "化湿"], contraindicated_methods=["苦寒伤脾"],
         span_note="脾湿者健脾以杜生湿之源，兼化已停之湿"),
    dict(when_nature=["气虚"], when_location=["脾"], principle="健脾益气",
         method_keywords=["健脾", "益气"], contraindicated_methods=["破气", "苦寒"],
         span_note="脾气虚者健脾益气以复运化"),
    dict(when_nature=["阳虚"], when_location=["脾"], principle="温中健脾",
         method_keywords=["温中", "健脾"], contraindicated_methods=["苦寒"],
         span_note="脾阳虚者温中健脾以散寒"),
    dict(when_nature=["寒"], when_location=["胃"], principle="温胃散寒",
         method_keywords=["温胃", "散寒"], contraindicated_methods=["苦寒"],
         span_note="胃寒者温中散寒以止痛"),
    dict(when_nature=["热"], when_location=["胃"], principle="清胃泻火",
         method_keywords=["清胃", "泻火"], contraindicated_methods=["温燥"],
         span_note="胃热者清胃泻火以降逆"),
    dict(when_nature=["阴虚"], when_location=["胃"], principle="养胃阴",
         method_keywords=["养胃阴", "滋阴"], contraindicated_methods=["温燥", "苦寒"],
         span_note="胃阴虚者甘凉濡润以养胃阴"),
    dict(when_nature=["气滞"], when_location=["胃"], principle="理气和胃",
         method_keywords=["理气", "和胃"], contraindicated_methods=["峻补壅滞"],
         span_note="胃气滞者理气和胃以降逆"),
    dict(when_nature=["热"], when_location=["肝"], principle="清肝泻火",
         method_keywords=["清肝", "泻火"], contraindicated_methods=["温燥"],
         span_note="肝热者清肝泻火"),
    dict(when_nature=["阴虚"], when_location=["肝"], principle="滋补肝肾",
         method_keywords=["滋补肝肾", "养阴"], contraindicated_methods=["辛燥"],
         span_note="肝阴虚者滋水涵木，肝肾同治"),
    dict(when_nature=["湿"], when_location=["胆"], principle="清利肝胆湿热",
         method_keywords=["清利", "利胆"], contraindicated_methods=["温补"],
         span_note="胆湿者清利肝胆湿热"),
    dict(when_nature=["热"], when_location=["胆"], principle="清胆泻火",
         method_keywords=["清胆", "泻火"], contraindicated_methods=["温燥"],
         span_note="胆热者清胆和胃泻火"),
    dict(when_nature=["痰"], when_location=["肺"], principle="宣肺化痰",
         method_keywords=["宣肺", "化痰"], contraindicated_methods=["敛肺止咳过早"],
         span_note="肺痰者宣肺以助化痰之力"),
    dict(when_nature=["热"], when_location=["肺"], principle="清肺泻热",
         method_keywords=["清肺", "泻热"], contraindicated_methods=["温燥"],
         span_note="肺热者清泄肺热"),
    dict(when_nature=["阴虚"], when_location=["肺"], principle="养阴润肺",
         method_keywords=["养阴", "润肺"], contraindicated_methods=["辛燥"],
         span_note="肺阴虚者甘凉濡润以养肺阴"),
    dict(when_nature=["痰"], when_location=["心"], principle="豁痰宁心",
         method_keywords=["豁痰", "宁心"], contraindicated_methods=["滋腻"],
         span_note="心痰者豁痰开窍以宁心神"),
    dict(when_nature=["热"], when_location=["心"], principle="清心泻火",
         method_keywords=["清心", "泻火"], contraindicated_methods=["温补"],
         span_note="心热者清心泻火以安神"),
    dict(when_nature=["阳虚"], when_location=["心"], principle="温补心阳",
         method_keywords=["温补心阳"], contraindicated_methods=["苦寒"],
         span_note="心阳虚者温补心阳以复脉"),
    dict(when_nature=["阳虚"], when_location=["肾"], principle="温补肾阳",
         method_keywords=["温补肾阳"], contraindicated_methods=["苦寒", "峻下"],
         span_note="肾阳虚者温补肾阳以固本"),
    dict(when_nature=["阴虚"], when_location=["肾"], principle="滋补肾阴",
         method_keywords=["滋补肾阴"], contraindicated_methods=["辛燥"],
         span_note="肾阴虚者滋补肾阴以填精"),
    dict(when_nature=["湿"], when_location=["肾"], principle="温阳利水",
         method_keywords=["温阳", "利水"], contraindicated_methods=["苦寒", "峻下"],
         span_note="肾湿（水泛）者温阳化气利水"),
    # ---- 脏腑生理特性决定的默认治疗方向（when_nature 空、when_location
    # 具体）。**这十条是给"病位定了、病性还没定/未标注"的情况兜底**——
    # 脾以升为健、胃以降为和这类是叶天士以来反复重申的脏腑生理特性，
    # 不依赖具体病性也能定出一个大方向，跟前面 12 条"普遍适用不看病位"的
    # 元治则正好互补（那 12 条要看病性、不看病位；这 10 条要看病位、
    # 不强求病性）。 ----
    dict(when_nature=[], when_location=["脾"], principle="脾宜升则健",
         method_keywords=["健脾", "升清"], contraindicated_methods=["峻下", "破气"],
         span_note="脾以升为健，健运中焦以复升清之职"),
    dict(when_nature=[], when_location=["胃"], principle="胃宜降则和",
         method_keywords=["和胃", "降逆"], contraindicated_methods=["峻补壅滞"],
         span_note="胃以降为和，通降胃气以复和降之职"),
    dict(when_nature=[], when_location=["肝"], principle="肝宜疏泄",
         method_keywords=["疏肝", "调畅气机"], contraindicated_methods=["峻补敛涩"],
         span_note="肝主疏泄，以调畅气机为治疗着眼点"),
    dict(when_nature=[], when_location=["胆"], principle="胆宜通降",
         method_keywords=["利胆", "通降"], contraindicated_methods=["峻补壅滞"],
         span_note="胆以通降为顺，以疏利胆腑为治疗着眼点"),
    dict(when_nature=[], when_location=["心"], principle="心宜安养",
         method_keywords=["养心", "安神"], contraindicated_methods=["峻烈攻伐"],
         span_note="心主血脉藏神，以养心安神为治疗着眼点"),
    dict(when_nature=[], when_location=["肺"], principle="肺宜宣降",
         method_keywords=["宣肺", "肃降"], contraindicated_methods=["峻下伤肺"],
         span_note="肺主宣发肃降，以恢复宣降之职为治疗着眼点"),
    dict(when_nature=[], when_location=["肾"], principle="肾宜固藏",
         method_keywords=["补肾", "固摄"], contraindicated_methods=["峻下攻伐"],
         span_note="肾主封藏为先天之本，以固护肾气为治疗着眼点"),
    dict(when_nature=[], when_location=["大肠"], principle="大肠宜通",
         method_keywords=["通腑", "导滞"], contraindicated_methods=["峻补涩肠"],
         span_note="大肠以通为用，以通导腑气为治疗着眼点"),
    dict(when_nature=[], when_location=["小肠"], principle="小肠宜分清泌浊",
         method_keywords=["调理受盛", "分清泌浊"], contraindicated_methods=["峻下"],
         span_note="小肠主受盛化物、分清泌浊，以调理受盛化物之职为治疗着眼点"),
    dict(when_nature=[], when_location=["三焦"], principle="三焦宜通利",
         method_keywords=["通利三焦", "调畅气机"], contraindicated_methods=["峻补壅滞"],
         span_note="三焦为水液气机运行之道路，以疏通三焦为治疗着眼点"),
]
assert len(TREATMENT_PRINCIPLES) >= 30, len(TREATMENT_PRINCIPLES)
# 配伍理论（D 类）。七情与君臣佐使构成规则、加减规则的 span 是从
# books/中药学.md、books/方剂学.md 摘出的**原文整段**（构建脚本会逐条校验
# span 确实是该书文件内容的子串，抽不到就报错、不静默通过）。
# 十八反十九畏本身**不复制**——那张表只在 core/safety_output.py 一处维护
# （CLAUDE.md「同一概念的匹配逻辑只能有一处实现」），这里只留一条指针型规则
# 说明"配伍禁忌复用 core.safety_output"，不重复枚举 24 对药名。
COMPATIBILITIES = [
    dict(relation="单行", definition="单用一味中药治疗病情单一的疾病，不需辅药",
         example_pairs=[], book="中药学",
         span_key="1.单行是指单用一味中药来治疗某种病情单一的疾病"),
    dict(relation="相须", definition="性能功效相类似的药物配合应用，可增强原有疗效",
         example_pairs=[["石膏", "知母"]], book="中药学",
         span_key="相须、相使可以起到协同作用，能提高药效"),
    dict(relation="相使", definition="性能功效有某些共性，以一药为主、一药为辅，能提高主药疗效",
         example_pairs=[], book="中药学",
         span_key="相须、相使可以起到协同作用，能提高药效"),
    dict(relation="相畏", definition="一种药物的毒烈之性能被另一种药物减轻或消除",
         example_pairs=[], book="中药学",
         span_key="相畏、相杀可以减轻或消除毒副作用，以保证安全用药"),
    dict(relation="相杀", definition="一种药物能减轻或消除另一种药物的毒烈之性",
         example_pairs=[], book="中药学",
         span_key="相畏、相杀可以减轻或消除毒副作用，以保证安全用药"),
    dict(relation="相恶", definition="两药合用，一种药物能使另一种药物原有功效降低甚至丧失",
         example_pairs=[], book="中药学",
         span_key="相恶则是因为中药的拮抗作用，抵消或减弱其中一种中药的功效"),
    dict(relation="相反", definition="两药合用能产生或增强毒性反应或强烈副作用，属配伍禁忌",
         example_pairs=[], book="中药学",
         span_key="相反则是中药相互作用，能产生或增强毒性反应或强烈的副作用。故相恶、相反是中医配伍用药的禁忌"),
    # ---- 君臣佐使构成规则（4 条） ----
    dict(relation="君药", definition="针对主病或主证起主要治疗作用的药物，是方中不可或缺、药力居首的药物",
         example_pairs=[], book="方剂学",
         span_key="君药是针对主病或主证起主要治疗作用的药物，是方中不可或缺，且药力居首的药物"),
    dict(relation="臣药", definition="辅助君药加强治疗主病主证的作用，或针对兼病兼证起治疗作用，药力小于君药",
         example_pairs=[], book="方剂学",
         span_key="臣药一是辅助君药加强治疗主病或主证作用的药物；二是针对兼病或兼证起治疗作用的药物。其在方中之药力小于君药"),
    dict(relation="佐药", definition="佐助君臣加强疗效或治兼证，或佐制君臣峻烈毒性，或反佐以防格拒，药力小于臣药",
         example_pairs=[], book="方剂学",
         span_key="佐药一是佐助药，即协助君、臣药以加强治疗作用，或直接治疗次要兼证的药物；二是佐制药"),
    dict(relation="使药", definition="引方中诸药达病所（引经药），或调和诸药，药力较小、用量亦轻",
         example_pairs=[], book="方剂学",
         span_key="使药一是引经药，即能引方中诸药以达病所的药物；二是调和药，即具有调和诸药作用的药物"),
    # ---- 组方原则（1 条：君药必备，臣佐使非齐备） ----
    dict(relation="组方原则", definition="君药是必备的而臣佐使药并非齐备；君药味数宜少（一般一味），臣药可多于君药，佐药常多于臣药，使药多为一味",
         example_pairs=[], book="方剂学",
         span_key="一首方剂中，君药是必备的，而臣、佐、使药并非齐备"),
    # ---- 药味加减规律（2 条） ----
    dict(relation="佐使药加减", definition="佐使药药力较小，在君药不变、主症不变的前提下加减以适应次要兼症",
         example_pairs=[], book="方剂学",
         span_key="佐使药的加减，因为佐使药在方中的药力较小，不至于引起该方功用的根本改变"),
    dict(relation="臣药加减", definition="改变臣药会改变方剂的主要配伍关系，使方剂功用发生较大变化",
         example_pairs=[], book="方剂学",
         span_key="臣药的加减，这种变化改变了方剂的主要配伍关系，使方剂的功用发生较大变化"),
    # ---- 药对经验（8 条，取自《方剂学》总论举例） ----
    dict(relation="药对", definition="桂枝配芍药以调和营卫，解肌发表",
         example_pairs=[["桂枝", "芍药"]], book="中药学",
         span_key="桂枝配芍药以调和营卫，解肌发表"),
    dict(relation="药对", definition="柴胡配黄芩以和解少阳，消退寒热",
         example_pairs=[["柴胡", "黄芩"]], book="中药学",
         span_key="柴胡配黄芩以和解少阳，消退寒热"),
    dict(relation="药对", definition="干姜配五味子以开阖并用，宣降肺气",
         example_pairs=[["干姜", "五味子"]], book="中药学",
         span_key="干姜配五味子以开阖并用，宣降肺气"),
    dict(relation="药对", definition="黄连配干姜以寒热并调，降阳和阴",
         example_pairs=[["黄连", "干姜"]], book="中药学",
         span_key="黄连配干姜以寒热并调，降阳和阴"),
    dict(relation="药对", definition="肉桂配黄连以交通心肾，水火互济",
         example_pairs=[["肉桂", "黄连"]], book="中药学",
         span_key="肉桂配黄连以交通心肾，水火互济"),
    dict(relation="药对", definition="黄芪配当归以阳生阴长，补气生血",
         example_pairs=[["黄芪", "当归"]], book="中药学",
         span_key="黄芪配当归以阳生阴长，补气生血"),
    dict(relation="药对", definition="熟地配附子以阴中求阳，阴阳并调",
         example_pairs=[["熟地", "附子"]], book="中药学",
         span_key="熟地配附子以阴中求阳，阴阳并调"),
    dict(relation="药对", definition="晚蚕沙配皂角子以升清降浊，滑肠通便",
         example_pairs=[["晚蚕沙", "皂角子"]], book="中药学",
         span_key="晚蚕沙配皂角子以升清降浊，滑肠通便"),
    dict(relation="药对", definition="积实配白术以寓消于补，消补兼施",
         example_pairs=[["积实", "白术"]], book="中药学",
         span_key="积实配白术以寓消于补，消补兼施"),
    dict(relation="十九畏", definition="十九畏是与十八反并列的另一类配伍禁忌，"
                                   "宋代以来从相畏名称使用混乱中逐渐独立提出",
         example_pairs=[], book="中药学",
         span_key="作为中药配伍禁忌的“十九畏”就是在这种情况下提出的"),
    # ---- 配伍禁忌指针（1 条，不复制表，只指向既有实现） ----
    dict(relation="配伍禁忌", definition="十八反、十九畏的具体药名表由 core.safety_output.INCOMPATIBLE_PAIRS "
                                   "唯一维护，本条只记录“存在配伍禁忌”这一判据本身，不复制表内容",
         example_pairs=[], book="中药学",
         span_key="目前医药界共同认可的中药配伍禁忌有“十八反”和“十九畏”"),
]
assert len(COMPATIBILITIES) >= 25, len(COMPATIBILITIES)


# ============================================================================
# 组装 + 出处校验 + 写文件
# ============================================================================

def _book_text(name: str) -> str:
    path = BOOKS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在——D 类规则的出处校验需要这本书在 books/ 里")
    return path.read_text(encoding="utf-8")


_BOOK_CACHE: dict[str, str] = {}


def _verify_span_in_book(book: str, span: str) -> None:
    """D 类规则的 span 必须是该书文件内容的子串。**抽不到就崩，不静默放行**
    ——防幻觉设计里"取不到出处的不收"这条铁律，对医理规则同样成立。"""
    if book not in _BOOK_CACHE:
        _BOOK_CACHE[book] = _book_text(f"{book}.md")
    if span not in _BOOK_CACHE[book]:
        raise ValueError(f"《{book}》里找不到这段文字（span 抽取有误或原文已变）：{span!r}")


def build_organ_relations() -> list[dict]:
    out = []
    for i, r in enumerate(ORGAN_RELATIONS, start=1):
        out.append({
            "id": f"ZX-{i:03d}", "kind": "organ_relation",
            "subject": r["subject"], "relation": r["relation"], "object": r["object"],
            "mechanism": r["mechanism"], "direction": r["direction"],
            "trigger_elements": r["trigger_elements"], "implied_elements": r["implied_elements"],
            "source": "人工整理（据中医基础理论藏象学说通用表述，本项目未持有该教材电子版）",
            "span": r["mechanism"], "confidence": "curated", "applies_to": APPLIES_TO_ORGAN,
        })
    return out


def build_pathomechanisms() -> list[dict]:
    out = []
    for i, r in enumerate(PATHOMECHANISMS, start=1):
        out.append({
            "id": f"BJ-{i:03d}", "kind": "pathomechanism",
            "from": r["from_"], "to": r["to"], "condition": r["condition"],
            "mechanism": r["mechanism"], "markers": r["markers"],
            "source": "人工整理（据中医基础理论病机学说通用表述，本项目未持有该教材电子版）",
            "span": r["mechanism"], "confidence": "curated", "applies_to": APPLIES_TO_ORGAN,
        })
    return out


def build_treatment_principles() -> list[dict]:
    out = []
    for i, r in enumerate(TREATMENT_PRINCIPLES, start=1):
        out.append({
            "id": f"ZZ-{i:03d}", "kind": "treatment_principle",
            "when_nature": r["when_nature"], "when_location": r["when_location"],
            "principle": r["principle"], "method_keywords": r["method_keywords"],
            "contraindicated_methods": r["contraindicated_methods"],
            "source": "人工整理（据历代治则理论通用表述，本项目未持有可校验页码的教材版本）",
            "span": r["span_note"], "confidence": "curated", "applies_to": APPLIES_TO_ORGAN,
        })
    return out


def build_compatibilities() -> list[dict]:
    out = []
    for i, r in enumerate(COMPATIBILITIES, start=1):
        _verify_span_in_book(r["book"], r["span_key"])
        out.append({
            "id": f"PW-{i:03d}", "kind": "compatibility",
            "relation": r["relation"], "definition": r["definition"],
            "example_pairs": r["example_pairs"],
            "source": f"《{r['book']}》总论", "span": r["span_key"],
            "confidence": "classic", "applies_to": APPLIES_TO_COMPAT,
        })
    return out


def build_rows() -> list[dict]:
    return (build_organ_relations() + build_pathomechanisms()
            + build_treatment_principles() + build_compatibilities())


def stats(rows: list[dict]) -> dict:
    by_kind: dict[str, int] = {}
    by_conf: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
    return {"total": len(rows), "by_kind": by_kind, "by_confidence": by_conf,
            "curated_ratio": round(by_conf.get("curated", 0) / len(rows), 3) if rows else 0.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="只打统计，不写文件")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    rows = build_rows()
    s = stats(rows)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if s["total"] == 0:
        print("一条规则都没生成", file=sys.stderr)
        return 1
    if args.stats:
        return 0
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"→ {path}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
