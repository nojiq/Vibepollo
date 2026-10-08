/**
 * @file src/platform/macos/remote_monitor_expiry.h
 * @brief Pure grace-period policy for retained Remote Monitor displays.
 */
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace platf::macos_virtual_display {
  /**
   * @brief Tracks one disconnect grace-period record per paired client.
   *
   * The owner/lifecycle code supplies synchronization and validates the owner
   * generation before releasing a display.  This class only answers which
   * generation's grace period has elapsed; it has no platform or thread
   * dependencies.
   */
  class remote_monitor_expiry_t {
  public:
    using clock = std::chrono::steady_clock;
    using generation_t = std::uint64_t;
    using expired_entry_t = std::pair<std::string, generation_t>;

    static constexpr std::chrono::seconds grace_period {30};

    /** Arm or replace the client's pending disconnect grace period. */
    void transport_lost(
      std::string_view client_uuid,
      generation_t generation,
      clock::time_point now
    ) {
      if (client_uuid.empty()) return;
      const auto existing = pending_.find(std::string {client_uuid});
      if (existing != pending_.end() && existing->second.generation > generation) return;
      pending_[std::string {client_uuid}] = pending_t {
        .generation = generation,
        .deadline = now + grace_period,
      };
    }

    /** Cancel any pending expiry when the client resumes. */
    void resumed(std::string_view client_uuid) {
      pending_.erase(std::string {client_uuid});
    }

    /** Forget all state for an unpaired client. */
    void forget(std::string_view client_uuid) {
      pending_.erase(std::string {client_uuid});
    }

    /** A delayed release must not cancel a newer launch's timer. */
    void forget(std::string_view client_uuid, generation_t generation) {
      const auto it = pending_.find(std::string {client_uuid});
      if (it != pending_.end() && it->second.generation == generation) pending_.erase(it);
    }

    /**
     * @brief Remove and return all records due at or before @p now.
     *
     * Replacing a record for the same UUID removes the older generation from
     * this policy, so a stale timer cannot be returned after a reconnect.
     */
    [[nodiscard]] std::vector<expired_entry_t> expired(clock::time_point now) {
      std::vector<expired_entry_t> result;
      for (auto it = pending_.begin(); it != pending_.end();) {
        if (it->second.deadline > now) {
          ++it;
          continue;
        }
        result.emplace_back(it->first, it->second.generation);
        it = pending_.erase(it);
      }
      return result;
    }

  private:
    struct pending_t {
      generation_t generation {};
      clock::time_point deadline {};
    };

    std::map<std::string, pending_t> pending_;
  };

  using remote_monitor_expiry_policy_t = remote_monitor_expiry_t;
}  // namespace platf::macos_virtual_display
