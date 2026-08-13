-- wrk 压测脚本：模拟 backend/scripts/traffic_upload_stress_test.py 的上报流程。
--
-- 推荐执行：
--   wrk -t8 -c10000 -d30s -s backend/scripts/traffic_upload_wrk.lua \
--     http://127.0.0.1:8000/api/traffic/upload -- 10000 8
--
-- 参数说明：
--   第 1 个 wrk 脚本参数：总请求数上限，0 表示按 -d 持续压测。
--   第 2 个 wrk 脚本参数：wrk 线程数，应与 -t 保持一致，用于更均匀地生成全局请求序号。
--
-- 可选环境变量：
--   TRAFFIC_UPLOAD_API_KEY：接口 x-api-key，默认 xiaohe666。
--   TRAFFIC_UPLOAD_REQUESTS：总请求数上限，优先级低于第 1 个脚本参数。
--   TRAFFIC_UPLOAD_THREADS：wrk 线程数，优先级低于第 2 个脚本参数。

local API_KEY = os.getenv("TRAFFIC_UPLOAD_API_KEY") or "xiaohe666"

local MAC_ADDRESSES = {
  "5e:9b:77:f6:58:f8",
  "2a:00:8b:ce:4e:34",
  "3e:06:26:a2:be:fe",
  "42:96:a2:03:60:9d",
  "00:1b:21:bc:3a:02",
  "00:1b:21:bc:3a:03",
  "00:1b:21:bc:3a:05",
  "00:1b:21:bc:3a:07",
  "56:69:80:0b:6b:28",
  "de:4f:00:ed:7e:a6",
  "da:2f:0e:f1:6a:51",
  "26:50:14:3f:e4:b3",
  "6a:59:0b:46:bd:05",
  "d6:12:65:82:20:5c",
  "fe:e8:ef:87:08:94",
  "a6:76:8b:62:5d:44",
  "12:0f:e1:d2:fd:5d",
  "26:eb:bc:02:91:61",
  "b6:cc:c8:5f:ad:e7",
  "62:14:1c:94:d2:7e",
  "ca:b3:1a:37:55:82",
  "fa:20:04:d2:d4:fa",
  "2e:71:1c:3b:be:3a",
  "72:cc:0d:0c:0c:ca",
  "be:08:e4:5e:a6:b6",
  "96:b1:ef:3d:74:5d",
  "fa:c6:56:96:f4:81",
  "9e:97:e8:94:af:7f",
  "8e:b7:3d:83:eb:fe",
  "3e:5d:da:84:67:3b",
  "32:e1:0e:0b:94:af",
  "ae:ae:82:7e:a2:29",
  "8a:b2:73:f4:ab:6e",
  "96:0c:a3:13:df:12",
  "b6:14:82:c4:56:2d",
  "a6:ba:e3:f0:3a:e0",
  "c6:6d:60:57:b1:4f",
  "76:8d:61:5c:19:2a",
  "0e:a3:ed:0e:ab:a8",
  "56:29:64:de:42:a0",
  "ae:c2:a5:24:25:bb",
  "bc:24:11:09:fe:2a",
  "fa:c0:21:d2:69:76",
  "7a:b6:b3:98:68:5d",
  "26:33:4d:9c:3a:c4",
  "fe:80:dc:39:bd:e6",
  "3e:bd:18:e8:1e:a2",
  "52:a7:df:d4:e5:39",
  "ce:8a:50:e2:84:48",
  "26:99:2a:a9:2c:8c",
  "ba:e6:93:84:ae:7d",
  "ce:64:ed:08:74:d1",
  "ce:cb:3f:8e:5c:2d",
  "d2:a5:af:5a:82:07",
  "82:88:13:d3:07:f0",
  "ca:8f:9b:b3:6b:bc",
  "22:0f:e9:f3:86:10",
  "86:95:ab:af:20:9c",
  "3e:02:17:a9:dc:16",
}

local thread_sequence = 0
local threads = {}
assigned_thread_id = 1
status_counts = {}
status_2xx = 0
status_4xx = 0
status_5xx = 0
status_other = 0
effective_thread_count = 1
effective_total_request_limit = 0
local thread_id = 1
local thread_count = tonumber(os.getenv("TRAFFIC_UPLOAD_THREADS")) or 1
local total_request_limit = tonumber(os.getenv("TRAFFIC_UPLOAD_REQUESTS")) or 0
local local_request_index = 0
local local_request_limit = 0

wrk.method = "POST"
wrk.headers["Content-Type"] = "application/json"
wrk.headers["x-api-key"] = API_KEY
wrk.headers["User-Agent"] = "pcdn-tx-traffic-wrk/1.0"

--- 功能：解析正整数配置值。
--- 参数：
---   value: 待解析的字符串或数值。
---   default_value: 解析失败时使用的默认值。
--- 返回值：
---   number: 解析后的非负整数。
local function parse_non_negative_integer(value, default_value)
  local parsed_value = tonumber(value)
  if parsed_value == nil or parsed_value < 0 then
    return default_value
  end
  return math.floor(parsed_value)
end

--- 功能：生成当前整 5 分钟 Unix 时间戳。
--- 参数：
---   无。
--- 返回值：
---   number: 当前时间向下取整到 5 分钟窗口后的 Unix timestamp 秒数。
local function build_time_id()
  local current_timestamp = os.time()
  return current_timestamp - current_timestamp % 300
end

--- 功能：为单次请求生成全局序号。
--- 参数：
---   无。
--- 返回值：
---   number: 基于线程编号和线程数生成的全局请求序号。
local function build_request_index()
  local request_index = (local_request_index * thread_count) + thread_id - 1
  local_request_index = local_request_index + 1
  return request_index
end

--- 功能：为单次请求生成不重复的整 5 分钟 time_id。
--- 参数：
---   request_index: 当前请求的全局序号。
--- 返回值：
---   number: 基于当前整 5 分钟窗口偏移后的 Unix timestamp 秒数。
local function build_request_time_id(request_index)
  local mac_slot_index = math.floor(request_index / #MAC_ADDRESSES)
  return build_time_id() - mac_slot_index * 300
end

--- 功能：构造 /api/traffic/upload 的 JSON 数组请求体。
--- 参数：
---   request_index: 当前请求的全局序号。
--- 返回值：
---   string: JSON 字符串，请求体包含 time_id、mac、rx_speed、tx_speed。
local function build_payload(request_index)
  local mac_address = MAC_ADDRESSES[(request_index % #MAC_ADDRESSES) + 1]
  local time_id = build_request_time_id(request_index)
  local rx_speed = math.random(1000000, 10000000)
  local tx_speed = math.random(100000, 2000000)

  return string.format(
    '[{"time_id":%d,"mac":"%s","rx_speed":%d,"tx_speed":%d}]',
    time_id,
    mac_address,
    rx_speed,
    tx_speed
  )
end

--- 功能：为每个 wrk 线程分配稳定编号。
--- 参数：
---   thread: wrk 线程对象。
--- 返回值：
---   nil: 只向线程对象写入 assigned_thread_id。
function setup(thread)
  thread_sequence = thread_sequence + 1
  threads[thread_sequence] = thread
  thread:set("assigned_thread_id", thread_sequence)
end

--- 功能：初始化当前 wrk 线程的压测参数。
--- 参数：
---   args: wrk 在 URL 后通过 -- 传入的脚本参数数组。
--- 返回值：
---   nil: 只初始化线程局部变量。
function init(args)
  thread_id = parse_non_negative_integer(_G.assigned_thread_id, 1)
  total_request_limit = parse_non_negative_integer(args[1], total_request_limit)
  thread_count = parse_non_negative_integer(args[2], thread_count)
  if thread_count < 1 then
    thread_count = 1
  end

  if total_request_limit > 0 then
    local_request_limit = math.ceil(total_request_limit / thread_count)
  end

  effective_thread_count = thread_count
  effective_total_request_limit = total_request_limit
  math.randomseed(os.time() + thread_id * 1000003)
end

--- 功能：生成单次 wrk HTTP 请求。
--- 参数：
---   无。
--- 返回值：
---   string: wrk 可发送的完整 HTTP 请求。
function request()
  local request_index = build_request_index()
  if local_request_limit > 0 and local_request_index >= local_request_limit then
    wrk.thread:stop()
  end
  return wrk.format(nil, nil, nil, build_payload(request_index))
end

--- 功能：记录每个 HTTP 响应状态码，便于发现非 2xx 响应。
--- 参数：
---   status: HTTP 响应状态码。
---   headers: HTTP 响应头。
---   body: HTTP 响应体。
--- 返回值：
---   nil: 只更新状态码计数。
function response(status, headers, body)
  status_counts[status] = (status_counts[status] or 0) + 1
  if status >= 200 and status < 300 then
    status_2xx = status_2xx + 1
  elseif status >= 400 and status < 500 then
    status_4xx = status_4xx + 1
  elseif status >= 500 and status < 600 then
    status_5xx = status_5xx + 1
  else
    status_other = status_other + 1
  end
end

--- 功能：压测结束后打印接口状态码分布和基础配置。
--- 参数：
---   summary: wrk 汇总数据。
---   latency: wrk 延迟统计对象。
---   requests: wrk 请求统计对象。
--- 返回值：
---   nil: 只向标准输出打印结果。
function done(summary, latency, requests)
  local merged_status_counts = {}
  local merged_2xx = 0
  local merged_4xx = 0
  local merged_5xx = 0
  local merged_other = 0
  local printed_thread_count = thread_count
  local printed_total_request_limit = total_request_limit

  for _, current_thread in ipairs(threads) do
    printed_thread_count = current_thread:get("effective_thread_count") or printed_thread_count
    printed_total_request_limit = current_thread:get("effective_total_request_limit") or printed_total_request_limit
    local current_status_counts = current_thread:get("status_counts")
    if type(current_status_counts) == "table" then
      for status, count in pairs(current_status_counts) do
        merged_status_counts[status] = (merged_status_counts[status] or 0) + count
      end
    end
    merged_2xx = merged_2xx + (current_thread:get("status_2xx") or 0)
    merged_4xx = merged_4xx + (current_thread:get("status_4xx") or 0)
    merged_5xx = merged_5xx + (current_thread:get("status_5xx") or 0)
    merged_other = merged_other + (current_thread:get("status_other") or 0)
  end

  io.write("\nTraffic upload wrk script summary\n")
  io.write(string.format("thread_count_arg: %d\n", printed_thread_count))
  io.write(string.format("total_request_limit: %d\n", printed_total_request_limit))
  io.write("status_classes:\n")
  io.write(string.format("  2xx: %d\n", merged_2xx))
  io.write(string.format("  4xx: %d\n", merged_4xx))
  io.write(string.format("  5xx: %d\n", merged_5xx))
  io.write(string.format("  other: %d\n", merged_other))
  io.write("status_codes:\n")
  for status, count in pairs(merged_status_counts) do
    io.write(string.format("  %s: %d\n", tostring(status), count))
  end
end
