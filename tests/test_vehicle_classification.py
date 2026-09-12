import pytest

from miit_gonggao.vehicle_classification import classify_vehicle


@pytest.mark.parametrize('model,name', [
    ('BJ7000', '纯电动轿车'), ('ZZ6120', '城市客车'), ('ZZ1040', '载货汽车'),
    ('BJ2043', '越野载货汽车'), ('ZZ5040', '冷藏车'), ('ZZ5120', '清障车'),
    ('ZZ5000', '汽车起重机'), ('ZZ4200', '半挂牵引车'),
    ('ZZ5001', '车厢可卸式垃圾车'), ('IMPORT', '多用途乘用车'),
    ('IMPORT', '纯电动乘用车（新能源汽车）'), ('ZZ5900', '全地面起重机'), ('ZZ5500', '修井机'),
    ('ZZ5000', '车载底盘式消防车'), ('ZZ5000', '中置轴挂车运输车'),
    ('ZZ5000', '汽车零部件运输车'), ('ZZ5000', '中置轴挂车运输车（新能源汽车）'),
    # 括号附注说明配套底盘/挂车，主体名称仍是整车；结尾锚点不得落到括号内容上。
    ('ZZ5000', '冷藏车（采用载货汽车底盘）'), ('ZZ5000', '消防车（二类底盘）'),
    ('ZZ5000', '厢式运输车（可牵引挂车）'), ('ZZ5000', '自卸汽车（不带挂车）'),
    ('ZZ5000', '冷藏车（底盘由某厂提供）'),
])
def test_complete_vehicles_need_no_pdf_seats_mass_or_numeric_subclass(model, name):
    result = classify_vehicle(model, name)
    assert result['inclusion_gate'] == 'accepted'
    assert 'vehicle_class' not in result
    assert classify_vehicle(model, name, {'gross_mass': '', 'passengers': ''}) == result


@pytest.mark.parametrize('category', ['乘用车', '插电式混合动力乘用车'])
@pytest.mark.parametrize('name', [
    '电动正三轮摩托车', '两轮摩托车', '三轮汽车', '低速货车', '仓栅式半挂车',
    '旅居挂车', '纯电动客车底盘', '载货汽车底盘', '消防车上装', '汽车车身',
    '车厢', '发动机总成', '汽车零部件',
    '纯电动客车底盘（三类）', '载货汽车底盘(二类)', '消防车上装（专用）',
    '汽车零部件（专用）', '非完整车辆', '非完整车辆载货汽车', '载货汽车（非完整车辆）',
    '仓栅式半挂车（专用）', '中置轴挂车（旅居）', '旅居挂车(新能源)',
])
def test_outside_products_are_excluded_even_with_automotive_catalog_category(name, category):
    assert classify_vehicle('ZZ7000', name, {'category': category})['inclusion_gate'] == 'excluded'


@pytest.mark.parametrize('model', ['CC6480BT05FPHEV', 'CC6480BT05GPHEV', 'CC6480BT25FPHEV'])
def test_official_compound_passenger_category_needs_no_product_name_or_pdf(model):
    evidence = {'category': '插电式混合动力乘用车'}
    assert classify_vehicle(model, '', evidence) == {
        'inclusion_gate': 'accepted', 'scope_reason': 'official_catalog_category',
    }
    assert classify_vehicle(model, '', {**evidence, 'dataTag': 'D'})['inclusion_gate'] == 'excluded'


@pytest.mark.parametrize('category', [
    '非道路乘用车', '非完整乘用车', '未知乘用车', '插电式混合动力乘用车底盘',
    '插电式混合动力商用车', '燃料电池商用车', '纯电动商用车',
])
def test_unrecognized_category_is_not_accepted_by_vehicle_suffix(category):
    assert classify_vehicle('ZZ7000', '', {'category': category})['inclusion_gate'] == 'pending_review'


def test_unknown_identity_is_not_inferred_from_model_or_data_tag():
    assert classify_vehicle('ZZ7000', '')['inclusion_gate'] == 'pending_review'
    assert classify_vehicle('ZZ7000', '', {'dataTag': 'Z'})['inclusion_gate'] == 'pending_review'
    assert classify_vehicle('XX', '', {'category': '乘用车'})['inclusion_gate'] == 'accepted'
    assert classify_vehicle('XX', '奥迪E创', {'category': '乘用车'})['inclusion_gate'] == 'accepted'
    assert classify_vehicle('XX', '未知产品')['inclusion_gate'] == 'pending_review'


def test_chassis_identity_is_distinct_from_a_complete_vehicle_using_chassis():
    assert classify_vehicle('XX', '载货汽车', {'dataTag': 'D'})['inclusion_gate'] == 'excluded'
    assert classify_vehicle('XX', '冷藏车', {
        'chassis': '载货汽车底盘', 'chassis_references': '[{"model_code":"ZZ1040"}]',
        'other': '上装由某厂制造，采用载货汽车底盘',
    })['inclusion_gate'] == 'accepted'
