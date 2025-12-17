import requests
import json
import time
import re
import sys
import random
import uuid
import concurrent.futures
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# 配置区域
# ==========================================

# 目标 URL
GRAPHQL_URL = "https://graphql.nova.is/graphql"

# 并发设置
CONCURRENT_WORKERS = 100  # 并发线程数（一次请求多少个）
BATCH_DELAY = 2         # 每批次间隔时间（秒），避免瞬间请求过多导致IP被Ban

# ==========================================
# [新增] 用户自定义筛选规则配置
# ==========================================

# 1. 连号与顺子开关
ENABLE_A4   = True      # 是否匹配 AAAA (4位连号, 如 7777, 2222)
ENABLE_A3   = True      # 是否匹配 AAA (3位连号, 如 777, 222)
ENABLE_ABC  = False     # 是否匹配 ABC (3位顺子, 如 123, 789)
ENABLE_ABCD = True      # 是否匹配 ABCD (4位顺子, 如 1234, 4321)

# 2. 自定义目标号码列表
# 只要号码中包含以下任意一个字符串，就会被认定为靓号
CUSTOM_TARGETS = [
    # 吉利数字示例
    "888", "666", "520", "1314",
    
    # 还可以输入您名字的数字谐音等
]

# 基础请求头
BASE_HEADERS = {
    "authority": "graphql.nova.is",
    "accept": "*/*",
    "accept-language": "is-IS",
    "content-type": "application/json",
    "origin": "https://portal.nova.is",
    "referer": "https://portal.nova.is/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"'
}

# ==========================================
# 全局控制 (用于优化显示)
# ==========================================
print_lock = threading.Lock()        # 确保打印不乱序
allow_printing = threading.Event()   # 控制是否允许打印进度
allow_printing.set()                 # 默认允许打印

# ==========================================
# 核心筛选规则逻辑
# ==========================================

def check_number_rules(phone_number):
    """
    检查号码是否符合规则
    """
    if not phone_number:
        return False, "号码为空"

    phone_number = str(phone_number)
    
    # 1. 优先检查：用户自定义的特定目标
    for target in CUSTOM_TARGETS:
        if target in phone_number:
            return True, f"符合自定义目标: 包含 '{target}'"
    
    # 2. 检查：A4 连号
    if ENABLE_A4 and re.search(r'(\d)\1{3,}', phone_number):
        return True, f"符合规则: A4连号 (发现连续4位重复数字)"

    # 3. 检查：A3 连号
    if ENABLE_A3 and re.search(r'(\d)\1{2,}', phone_number):
        return True, f"符合规则: A3连号 (发现连续3位重复数字)"

    # 准备顺子序列
    forward_seq = "0123456789"
    backward_seq = "9876543210"

    # 4. 检查：5位连续数字
    for i in range(len(forward_seq) - 4):
        if forward_seq[i:i+5] in phone_number:
            return True, f"符合规则: 正向5位连号 ({forward_seq[i:i+5]})"
    for i in range(len(backward_seq) - 4):
        if backward_seq[i:i+5] in phone_number:
            return True, f"符合规则: 反向5位连号 ({backward_seq[i:i+5]})"

    # 5. 检查：ABCD (4位顺子)
    if ENABLE_ABCD:
        for i in range(len(forward_seq) - 3):
            if forward_seq[i:i+4] in phone_number:
                return True, f"符合规则: 正向4位连号 ({forward_seq[i:i+4]})"
        for i in range(len(backward_seq) - 3):
            if backward_seq[i:i+4] in phone_number:
                return True, f"符合规则: 反向4位连号 ({backward_seq[i:i+4]})"

    # 6. 检查：ABC (3位顺子)
    if ENABLE_ABC:
        for i in range(len(forward_seq) - 2):
            if forward_seq[i:i+3] in phone_number:
                return True, f"符合规则: 正向3位连号 ({forward_seq[i:i+3]})"
        for i in range(len(backward_seq) - 2):
            if backward_seq[i:i+3] in phone_number:
                return True, f"符合规则: 反向3位连号 ({backward_seq[i:i+3]})"

    return False, "普通号码"

# ==========================================
# API 客户端类 (支持并发Session)
# ==========================================

class NovaClient:
    def __init__(self):
        self.session = requests.Session()
        
        # 配置连接池，实现TCP复用
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            pool_connections=CONCURRENT_WORKERS, 
            pool_maxsize=CONCURRENT_WORKERS,
            max_retries=retry_strategy
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self.session.headers.update(BASE_HEADERS)

    def get_dynamic_headers(self):
        """生成动态请求头"""
        headers = BASE_HEADERS.copy()
        random_uuid = str(uuid.uuid4())
        headers["request-context"] = f"appId=cid-v1:{random_uuid}"
        return headers

    def post_graphql(self, payload, headers=None):
        """发送 GraphQL 请求"""
        try:
            if headers is None:
                headers = self.get_dynamic_headers()
            
            response = self.session.post(
                GRAPHQL_URL, 
                json=payload, 
                headers=headers, 
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return None

    def search_single_number(self):
        """[并发任务] 执行一次号码查询"""
        payload = {
            "operationName": "AvailablePhoneNumbers",
            "variables": {
                "input": {
                    "count": 1,
                    "type": "Normal"
                }
            },
            "query": """query AvailablePhoneNumbers($input: SearchPhoneNumber) {
              availablePhoneNumbers(input: $input) {
                phoneNumber
                type
                __typename
              }
            }"""
        }

        data = self.post_graphql(payload)
        
        if data and 'data' in data and 'availablePhoneNumbers' in data['data']:
            numbers_list = data['data']['availablePhoneNumbers']
            if numbers_list and len(numbers_list) > 0:
                return numbers_list[0]['phoneNumber'], data
        
        return None, None

    # =========================================================
    # 以下为全功能方法（锁定、提交信息、确认订单）
    # 目前在 Main 中暂不调用，但已恢复供备用
    # =========================================================

    def create_cart_and_lock(self, phone_number):
        """
        [锁定专用] 只有找到靓号时才调用
        1. 创建新购物车 (获取独立 CartID)
        2. 锁定号码
        返回: (lock_response_data, cart_id)
        """
        print(f"[*] [锁定阶段] 正在为号码 {phone_number} 创建独立购物车...")
        
        # 1. 初始化购物车
        init_payload = {
            "operationName": "addMobileSignupToCart",
            "variables": {
                "input": {
                    "item": {
                        "variantId": "frelsi-oskrad-ferdamadur",
                        "quantity": 1,
                        "purchaseInfo": {}
                    },
                    "cartId": ""
                }
            },
            "query": """mutation addMobileSignupToCart($input: AddToCartInput!) {
              addToCart(input: $input) {
                cart {
                  id
                  items { id }
                }
              }
            }"""
        }
        
        init_data = self.post_graphql(init_payload)
        cart_id = None
        item_id = None
        
        if init_data and 'data' in init_data and 'addToCart' in init_data['data']:
            cart_data = init_data['data']['addToCart']['cart']
            cart_id = cart_data['id']
            if cart_data['items']:
                item_id = cart_data['items'][0]['id']
        
        if not cart_id:
            print("[-] 购物车创建失败，无法锁定。")
            return None, None

        # 2. 提交锁定
        lock_payload = {
            "operationName": "addMobileSignupToCart",
            "variables": {
                "input": {
                    "item": {
                        "variantId": "farsimi-otakmarkad-ferdamadur-1",
                        "quantity": 1,
                        "purchaseInfo": {
                            "service": {
                                "mobileSignupRightHolder": None,
                                "phoneNumber": phone_number,
                                "isNewNumber": True,
                                "type": "Mobile",
                                "isUnregistered": True,
                                "user": {
                                    "name": "Distant Traveller",
                                    "nationalId": "9999999999",
                                    "phoneNumber": phone_number
                                }
                            },
                            "contract": {
                                "cartItemId": item_id,
                                "type": "New"
                            }
                        }
                    },
                    "cartId": cart_id
                }
            },
            "query": """mutation addMobileSignupToCart($input: AddToCartInput!) {
              addToCart(input: $input) {
                cart {
                  id
                  isValid
                  items {
                    purchaseInfo {
                      service {
                        phoneNumber
                      }
                    }
                  }
                }
                error {
                  message
                }
              }
            }"""
        }

        lock_data = self.post_graphql(lock_payload)
        return lock_data, cart_id

    def submit_contact_info(self, cart_id, phone_number, email="2002xuanying@gmail.com"):
        """
        提交联系人信息
        """
        print(f"[*] [联系人信息] 正在提交邮箱 {email} ...")
        
        payload = {
            "operationName": "addContactInfo",
            "variables": {
                "input": {
                    "cartId": cart_id,
                    "contactInfo": {
                        "email": email,
                        "msisdn": phone_number,
                        "ssn": "9999999999",
                        "name": "Distant Traveller",
                        "address": "Lágmúli 9",
                        "zip": "105"
                    }
                }
            },
            "query": """mutation addContactInfo($input: AddContactInfoInput!) {
              addContactInfo(input: $input) {
                cart {
                  id
                  items {
                    id
                    variantId
                    purchaseInfo {
                      service {
                        phoneNumber
                      }
                    }
                  }
                  contact {
                    email
                    msisdn
                  }
                }
                error {
                  code
                  message
                }
              }
            }"""
        }
        
        return self.post_graphql(payload)

    def update_cart_item(self, cart_id, cart_items, phone_number, email="2002xuanying@gmail.com"):
        """
        [确认步骤] 更新购物车条目，绑定联系人信息
        """
        print(f"[*] [确认订单] 正在更新购物车条目...")
        
        main_item_id = None
        contract_item_id = None
        
        # 智能解析 ID
        for item in cart_items:
            vid = item.get('variantId', '')
            # 主商品 Day Pass
            if 'farsimi-otakmarkad-ferdamadur' in vid:
                main_item_id = item['id']
            # 关联商品 Travel Pack
            elif 'frelsi-oskrad-ferdamadur' in vid:
                contract_item_id = item['id']
        
        if not main_item_id or not contract_item_id:
            print(f"[-] 自动解析 Item ID 失败。Main: {main_item_id}, Contract: {contract_item_id}")
            return None

        payload = {
            "operationName": "updateCartItem",
            "variables": {
                "input": {
                    "cartId": cart_id,
                    "item": {
                        "quantity": 1,
                        "variantId": "farsimi-otakmarkad-ferdamadur-1",
                        "id": main_item_id,
                        "purchaseInfo": {
                            "contract": {
                                "cartItemId": contract_item_id,
                                "type": "New"
                            },
                            "service": {
                                "type": "Mobile",
                                "phoneNumber": phone_number,
                                "isNewNumber": True,
                                "isUnregistered": True,
                                "portInDate": "0001-01-01T00:00:00",
                                "roofAmount": None,
                                "departmentId": None,
                                "invoiceExplanation": None,
                                "mobileSignupRightHolder": None,
                                "user": {
                                    "name": "Distant Traveller",
                                    "nationalId": "9999999999",
                                    "email": email,
                                    "phoneNumber": phone_number
                                }
                            }
                        }
                    }
                }
            },
            "query": """mutation updateCartItem($input: UpdateCartItemInput!) {
              updateCartItem(input: $input) {
                cart {
                  id
                  isValid
                  items {
                    id
                  }
                }
                error {
                  code
                  message
                }
              }
            }"""
        }
        
        return self.post_graphql(payload)

# ==========================================
# 主程序逻辑
# ==========================================

def worker_task(client):
    """单个线程的工作逻辑"""
    try:
        # 1. 查询号码
        number, raw_response = client.search_single_number()
        
        if number:
            # 2. 检查规则
            is_good, reason = check_number_rules(number)
            
            if is_good:
                return {
                    "status": "FOUND",
                    "number": number,
                    "reason": reason,
                    "response": raw_response
                }
        return {"status": "RETRY"}
    except Exception as e:
        return {"status": "ERROR"}

def main():
    print("=== Nova 号码高并发筛选工具 (只读模式 + 全功能代码) ===")
    print(f"[*] 配置: 并发数 {CONCURRENT_WORKERS}, 使用 TCP 连接池复用")
    print("[*] 策略: 发现靓号后直接打印响应体，【不自动锁定】")
    print("[*] 提示: 锁定/下单相关函数已完整包含在 NovaClient 类中，如有需要可自行调用")
    print(f"[*] 自定义规则: 已加载 {len(CUSTOM_TARGETS)} 个自定义目标")
    
    client = NovaClient()
    
    total_attempts = 0
    
    # 使用线程池
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        while True:
            # 提交一批任务
            futures = [executor.submit(worker_task, client) for _ in range(CONCURRENT_WORKERS)]
            
            # 批次内计数器
            completed_in_batch = 0

            # 等待结果
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                total_attempts += 1
                completed_in_batch += 1
                
                # 实时打印进度条
                if allow_printing.is_set():
                    sys.stdout.write(f"\r[*] 正在筛选... 总计扫描: {total_attempts} 次 | 本轮进度: {completed_in_batch}/{CONCURRENT_WORKERS}")
                    sys.stdout.flush()

                if result["status"] == "FOUND":
                    # 暂停打印进度，防止刷屏干扰
                    allow_printing.clear()
                    
                    with print_lock:
                        print("\n\n" + "="*50)
                        print(f"[!!!] 发现符合要求的号码: {result['number']}")
                        print(f"[!!!] 匹配规则: {result['reason']}")
                        print("="*50)
                        print("[+] 原始响应体 (Raw Response):")
                        # 直接打印完整的 JSON 响应体
                        print(json.dumps(result['response'], indent=4))
                        print("="*50)
                    
                    # [交互] 暂停脚本，方便用户查看
                    sys.stdout.flush()
                    print(f"\n[⏸] 脚本已暂停。")
                    user_input = input(f">>> 按 'Enter' 或 'c' 继续搜索下一个，输入 'q' 退出: ").strip().lower()

                    if user_input == 'q':
                        print("[*] 用户选择退出。")
                        executor.shutdown(wait=False)
                        sys.exit(0)
                    
                    # 默认行为(Enter/c): 恢复打印，继续搜索
                    print("[*] 继续筛选中...")
                    allow_printing.set()
            
            # 批次间隔
            time.sleep(BATCH_DELAY)

if __name__ == "__main__":
    main()
