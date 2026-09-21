"""Shared product-source category SQL rules."""
def category_case(platform):
    if platform=='TM':
        return """CASE WHEN category_level_1='美容护肤/美体/精油' AND category_level_2='男士面部护理' THEN 'MEX'
        WHEN category_level_1='美容护肤/美体/精油' THEN 'SKIN'
        WHEN category_level_1='美发护发/假发' THEN 'HAIR'
        WHEN category_level_1='彩妆/香水/美妆工具' THEN 'MAKEUP' ELSE 'UNKNOWN' END"""
    if platform=='DY':
        return """CASE WHEN `商品二级分类`='美容护肤' AND `商品三级分类`='男士护肤' THEN 'MEX'
        WHEN `商品二级分类`='美容护肤' THEN 'SKIN'
        WHEN `商品三级分类` IN ('洗发护发','染发烫发/头发造型','男士美发护发/假发') THEN 'HAIR'
        WHEN `商品二级分类`='彩妆/香水/美妆工具' THEN 'MAKEUP'
        WHEN `商品一级分类`='个护家清' AND `商品三级分类` IN ('手部护理','身体护理','奢品护肤') THEN 'SKIN'
        ELSE 'UNKNOWN' END"""
    raise ValueError('Product/channel source not supported for platform')

