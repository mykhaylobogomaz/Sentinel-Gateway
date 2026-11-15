"""Atomic Redis Lua scripts for distributed rate limiting.

Executing check-and-update logic inside Redis Lua scripts guarantees atomicity
and prevents race conditions across concurrent gateway replicas.
"""

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call("HMGET", key, "tokens", "last_refill")
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
else
    local elapsed = math.max(0.0, now - last_refill)
    tokens = math.min(capacity, tokens + (elapsed * refill_rate))
    last_refill = now
end

local allowed = 0
local retry_after = 0.0

if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    local deficit = requested - tokens
    if refill_rate > 0 then
        retry_after = deficit / refill_rate
    else
        retry_after = 60.0
    end
end

local ttl = 60
if refill_rate > 0 then
    ttl = math.max(1, math.ceil((capacity / refill_rate) * 2))
end

redis.call("HMSET", key, "tokens", tostring(tokens), "last_refill", tostring(last_refill))
redis.call("EXPIRE", key, ttl)

local reset_seconds = 0.0
if refill_rate > 0 then
    reset_seconds = (capacity - tokens) / refill_rate
end

return {allowed, math.floor(tokens), tostring(reset_seconds), tostring(retry_after)}
"""

SLIDING_WINDOW_LUA = """
local prev_key = KEYS[1]
local curr_key = KEYS[2]
local window_size = tonumber(ARGV[1])
local max_requests = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local prev_count = tonumber(redis.call("GET", prev_key) or "0")
local curr_count = tonumber(redis.call("GET", curr_key) or "0")

local current_window_start = math.floor(now / window_size) * window_size
local elapsed_in_window = now - current_window_start
local prev_weight = math.max(0.0, (window_size - elapsed_in_window) / window_size)

local estimated_count = (prev_count * prev_weight) + curr_count
local reset_seconds = math.max(0.0, window_size - elapsed_in_window)

if estimated_count + 1 <= max_requests then
    curr_count = redis.call("INCR", curr_key)
    redis.call("EXPIRE", curr_key, window_size * 2)
    local weighted = (prev_count * prev_weight) + curr_count
    local remaining = math.max(0, math.floor(max_requests - weighted))
    return {1, remaining, tostring(reset_seconds), "0.0"}
else
    local retry_after = math.max(0.1, reset_seconds)
    return {0, 0, tostring(reset_seconds), tostring(retry_after)}
end
"""
