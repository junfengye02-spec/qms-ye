"""Create an idempotent Chinese warehouse dataset for the local InvenTree demo."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction

from part.models import Part, PartCategory
from stock.models import StockItem, StockLocation


def get_or_create_location(name, description, parent=None, structural=False):
    location = StockLocation.objects.filter(name=name, parent=parent).first()

    if location is None:
        location = StockLocation(
            name=name,
            description=description,
            parent=parent,
            structural=structural,
        )
        location.save()

    return location


def get_or_create_category(name, description, parent=None, default_location=None):
    category = PartCategory.objects.filter(name=name, parent=parent).first()

    if category is None:
        category = PartCategory(
            name=name,
            description=description,
            parent=parent,
            default_location=default_location,
        )
        category.save()

    return category


def get_or_create_part(*, ipn, name, description, category, location, barcode, minimum_stock):
    part = Part.objects.filter(IPN=ipn).first()

    if part is None:
        part = Part(
            IPN=ipn,
            name=name,
            description=description,
            category=category,
            default_location=location,
            units='pcs',
            minimum_stock=Decimal(str(minimum_stock)),
            active=True,
            purchaseable=True,
            salable=True,
        )
        part.save()

    if part.barcode_data != barcode:
        part.assign_barcode(barcode_data=barcode)

    return part


def get_or_create_stock(*, part, location, batch, quantity, user):
    stock_item = StockItem.objects.filter(part=part, batch=batch).first()

    if stock_item is None:
        stock_item = StockItem(
            part=part,
            location=location,
            batch=batch,
            quantity=Decimal(str(quantity)),
            packaging='箱装',
        )
        stock_item.save(user=user, notes='初始化中文演示库存')

    return stock_item


with transaction.atomic():
    admin = get_user_model().objects.get(username='admin')

    warehouse = get_or_create_location(
        '成品仓', '扫码入库演示仓库', structural=True
    )
    area_a = get_or_create_location(
        'A区', '电子成品存储区', parent=warehouse, structural=True
    )
    area_b = get_or_create_location(
        'B区', '配件和包装存储区', parent=warehouse, structural=True
    )
    shelf_a01 = get_or_create_location(
        'A-01货架', '控制器和传感器', parent=area_a
    )
    shelf_a02 = get_or_create_location(
        'A-02货架', '工业网关', parent=area_a
    )
    shelf_b01 = get_or_create_location(
        'B-01货架', '电源配件', parent=area_b
    )
    shelf_b02 = get_or_create_location(
        'B-02货架', '包装耗材', parent=area_b
    )
    pending = get_or_create_location('待检区', '待质量检验的入库成品')

    finished_goods = get_or_create_category(
        '成品物料', '成品仓扫码入库演示物料'
    )
    electronics = get_or_create_category(
        '电子产品', '控制器、传感器和网关', finished_goods, shelf_a01
    )
    accessories = get_or_create_category(
        '配件', '产品配套附件', finished_goods, shelf_b01
    )
    packaging = get_or_create_category(
        '包装耗材', '成品包装材料', finished_goods, shelf_b02
    )

    products = [
        {
            'ipn': 'CP-1001',
            'name': '智能控制器',
            'description': '成品仓扫码演示用智能控制器',
            'category': electronics,
            'location': shelf_a01,
            'barcode': '880000000001',
            'minimum_stock': 20,
            'stocks': [
                (shelf_a01, 'RK-20260824-001', 30),
                (shelf_a01, 'RK-20260824-002', 20),
            ],
        },
        {
            'ipn': 'CP-1002',
            'name': '温湿度传感器',
            'description': '仓储环境监测传感器',
            'category': electronics,
            'location': shelf_a01,
            'barcode': '880000000002',
            'minimum_stock': 50,
            'stocks': [(shelf_a01, 'RK-20260824-003', 120)],
        },
        {
            'ipn': 'CP-1003',
            'name': '工业物联网网关',
            'description': '工业设备联网和数据采集网关',
            'category': electronics,
            'location': shelf_a02,
            'barcode': '880000000003',
            'minimum_stock': 15,
            'stocks': [(shelf_a02, 'RK-20260824-004', 30)],
        },
        {
            'ipn': 'CP-2001',
            'name': '24V电源适配器',
            'description': '成品配套电源适配器',
            'category': accessories,
            'location': shelf_b01,
            'barcode': '880000000004',
            'minimum_stock': 30,
            'stocks': [(shelf_b01, 'RK-20260824-005', 80)],
        },
        {
            'ipn': 'CP-3001',
            'name': '标准包装纸箱',
            'description': '成品出库标准包装纸箱',
            'category': packaging,
            'location': shelf_b02,
            'barcode': '880000000005',
            'minimum_stock': 100,
            'stocks': [(shelf_b02, 'RK-20260824-006', 200)],
        },
        {
            'ipn': 'CP-9001',
            'name': '待检控制器',
            'description': '演示待检状态的入库产品',
            'category': electronics,
            'location': pending,
            'barcode': '880000000006',
            'minimum_stock': 5,
            'stocks': [(pending, 'RK-20260824-007', 10)],
        },
    ]

    for product in products:
        part = get_or_create_part(
            ipn=product['ipn'],
            name=product['name'],
            description=product['description'],
            category=product['category'],
            location=product['location'],
            barcode=product['barcode'],
            minimum_stock=product['minimum_stock'],
        )

        for location, batch, quantity in product['stocks']:
            get_or_create_stock(
                part=part,
                location=location,
                batch=batch,
                quantity=quantity,
                user=admin,
            )

    print('演示数据已就绪')
    print(f'物料分类: {PartCategory.objects.filter(name__in=["成品物料", "电子产品", "配件", "包装耗材"]).count()}')
    print(f'仓库库位: {StockLocation.objects.filter(name__in=["成品仓", "A区", "B区", "A-01货架", "A-02货架", "B-01货架", "B-02货架", "待检区"]).count()}')
    print(f'演示物料: {Part.objects.filter(IPN__startswith="CP-").count()}')
    print(f'库存批次: {StockItem.objects.filter(part__IPN__startswith="CP-").count()}')
    print(f'库存总数: {sum(item.quantity for item in StockItem.objects.filter(part__IPN__startswith="CP-"))}')
