"""
Parsing EANCOM XML Formats
https://www.gs1.org/standards/eancom/guideline/eancom-edition-2016

"""
from lxml import etree
from datetime import datetime

def parse_date(date, format_code):
    """"
    Parse C507 segment, DTM - Date/time/period
    102 = YYYYMMDD
    203 = YYYYMMDDHHMM
    """
    if format_code == '102':
        dt = datetime.strptime(date, '%Y%m%d')
        return dt.strftime('%d.%m.%y')
    elif format_code == '203':
        dt = datetime.strptime(date, '%Y%m%d%H%M')
        return dt.strftime('%d.%m.%y')
    else:
        return date

# Processing C507 segment to datetime value by specified format
def get_date_from_node(node):
    format = node.find('E2379').text
    date_str = node.find('E2380').text
    return parse_date(date_str, format)

def xpath_string_value(doc, path):
    value = doc.xpath(path)
    if isinstance(value, list):
        if len(value) == 1:
            return value[0].text
        elif len(value) == 0:
            return ''

# Process NAD segment, Name and address, To specify the name/address and their related function
def parse_nad_node(node):
    res = {}
    res['GLN']  = xpath_string_value(node, 'C082/E3039')
    res['Name'] = xpath_string_value(node, 'C080/E3036')
    res['Address'] = (xpath_string_value(node, 'E3207') + ',' + xpath_string_value(node, 'E3251') +
                      ',' + xpath_string_value(node, 'E3164') + ',' + xpath_string_value(node, 'C059/E3042') )
    return res


def get_AAE_price(prices: list):
    if 'AAE' in prices:
        return prices['AAE'] # Если цена с НДС есть - вернем её
    elif 'AAA' in prices:
        return prices['AAA'] * (100 + prices['VAT']) / 100 # Иначе посчитаем от цены без НДС + НДС


def parse_order(filename = None, xml_content = None):
    # https://www.gs1.org/standards/edi-xml-gs1-eancom/eancom-orders-s4/syntax-4

    if isinstance(filename, str):
        doc = etree.parse(filename)
    elif isinstance(xml_content, str) or isinstance(xml_content, bytes):
        doc = etree.fromstring(xml_content)

    res = {}
    header = {}
    header['Type']   = xpath_string_value(doc, '/ORDERS/BGM/C002/E1001') #тип документа
    header['Number'] = xpath_string_value(doc, '/ORDERS/BGM/C106/E1004') # номер заказа
    header['Status'] = int(xpath_string_value(doc, '/ORDERS/BGM/E1225'))  # Статус заказа

    order_date = doc.xpath('/ORDERS/DTM/C507[E2005="137"]') # Дата заказа
    delivery_date = doc.xpath('/ORDERS/DTM/C507[E2005="2"]')  # Дата доставки

    if len(order_date) == 1:
        header['Date'] = get_date_from_node(order_date[0])
    if len(delivery_date) == 1:
        header['Delivery_date'] = get_date_from_node(delivery_date[0])

    header['Comment'] = ''
    comments = doc.xpath('/ORDERS/FTX/C108/E4440') # Примечание
    for comment in comments:
        header['Comment'] = header['Comment'] + '; ' + comment.text

    parties = {}
    nads = doc.xpath('/ORDERS/SG2/NAD') #  Name and address
    for nad in nads:
        id = nad.find('E3035').text
        parties[id] = parse_nad_node(nad)

    """
        SU - идентификатор поставщика
        BY - идентификатор покупателя
        DP - идентификатор места доставки
        UD - идентификатор конечного места доставки
        IV - идентификатор плательщика
    """
    contract_number = xpath_string_value(doc, '/ORDERS/SG1/RFF/C506[E1153="CT"]/E1154')
    contract_date = ''
    if contract_number != '':
        contract_date = get_date_from_node(doc.xpath('/ORDERS/SG1/DTM/C507')[0])
    header['Contract'] = {'Number': contract_number, 'Date': contract_date}

    lines = []
    cnts = doc.xpath('/ORDERS/SG28')
    for cnt in cnts:
        line = {}
        line['Index'] = xpath_string_value(cnt, 'LIN/E1082') # номер строки
        line['GTIN']  = xpath_string_value(cnt, 'LIN/C212/E7140') # GS1 GTIN. 14 symbols. GTIN = '0' + EAN13
        line['BuyerCode'] = xpath_string_value(cnt, 'PIA[E4347="1"]/C212[E7143="IN"]/E7140')  # Код покупателя
        line['SupplierCode'] = xpath_string_value(cnt, 'PIA[E4347="1"]/C212[E7143="SA"]/E7140')  # Код поставщика
        line['Name'] = xpath_string_value(cnt, 'IMD/C273/E7008')
        line['Quantity'] = xpath_string_value(cnt, 'QTY/C186[E6063="21"]/E6060') # 21 - заказанное кол-во.
        line['QuantityPackage'] = xpath_string_value(cnt, 'QTY/C186[E6063="59"]/E6060')  # 59 - кол-во упаковок (паллет?)

        line['MonetaryAmount'] = []
        """ 
        MOA - этот раздел используется в двух целях: во-первых, когда речь идет о скидках/сборах, для указания 
        чистых сумм по строке, и, во-вторых, для предоставления оценочных сумм, например, таможенной стоимости.        
        Не знаю, нужен он или нет, тем более, что ниже идёт блок PRI Price details, но пусть будет.
        """
        moneys = cnt.xpath('MOA/C516')
        if len(moneys) > 0: # блока MOA может и не быть
            for money in moneys:
                line['MonetaryAmount'].append({'Type': xpath_string_value(money, 'E5025'), 'Amount': xpath_string_value(money, 'E5004')})

        line['Price'] = []
        # SG38-TAX Информация по налогам и бонусам (выплатам покупателю)
        # 5 = Бонусы покупателю,  7 = Налоги
        # VAT = НДС, GST = сервисные сборы, IMP = Пошлины на импорт
        vat = cnt.xpath('SG38/TAX[E5283="7"][C241/E5153="VAT"]/C243/E5278')[0].text

        # AAA - Цена без НДС, AAE - Цена с НДС, PRI -  Price details,
        AAA = {}
        AAE = {}
        prices = cnt.xpath('SG32/PRI/C509')
        for price in prices:
            price_type = xpath_string_value(price, 'E5125')
            rec ={'Type': price_type, 'Price': float(xpath_string_value(price, 'E5118')), 'VAT': float(vat)}
            if price_type == 'AAE':
                AAE = rec
            if price_type == 'AAA':
                AAA = rec
            line['Price'].append(rec)

        # Добавим цену AAE (Цена с НДС), если контрагент её не указывает
        if (len(AAE) == 0) and (len(AAA) > 0):
            AAE = AAA.copy()
            AAE['Price'] = AAE['Price'] * (100 + AAE['VAT']) / 100
            AAE['Type'] = 'AAE'
            line['Price'].append(AAE)

        lines.append(line)

    header['Partners'] = parties
    res['Header'] = header
    res['Content'] = lines
    return res
