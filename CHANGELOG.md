
## Changelog

### [1.0.11] - Online Since timestamp + uptime disabled by default
- New "Online Since" sensor: a stable boot-time timestamp derived from
  sysUpTime that only moves on a real reboot (poll/clock jitter inside a 5s
  window is absorbed), so it doesn't churn the recorder.
- Uptime sensor is now disabled by default — its value advances every poll
  and was a steady source of needless recorder inserts. Existing
  installations keep their current enable/disable state; new installs get
  "Online Since" instead.

### [1.0.10] - ifLastChange accelerator actually works
- The Repairs auto-manager's ifLastChange accelerator (back-date a long-down
  port's grace clock so it is flagged on the first poll instead of waiting a
  fresh grace period) was dormant in production: it reads
  `last_change_seconds` from the per-port data, but the coordinator only ever
  emitted the `last_change` datetime. The coordinator now also emits
  `last_change_seconds` (derived fresh each poll from the cached datetime), so
  the accelerator fires as designed.

### [1.0.9] - Flapping ports no longer nag
- Auto-manage Repairs: a port that flaps — a single down→up→down (or
  up→down→up) bounce within half the down-grace window — is recognised as an
  unstable link, not a stale dead port: the "port down" warning is no longer
  raised, and an already-open one is retracted. Self-corrects once the link
  stays quiet for that window.
- New Repair fix-flow option "Allow this port to flap": mutes a port
  explicitly until it is cleanly up for `up_restore_cycles` consecutive polls.

### [1.0.0] - Card improvement requests
- Button on card for switching between color schemes live on the spot (POE still absent)
- Port layout option added to show row of even/uneven to match HW look
- system box sensor override possibility
- respect HA theme colors
- Again a bit more hardened entity protection (needs more work)

### [0.9.7] - SNMP Port config
- Added SNMP port configuration option
- Fix for future deprecation warning
- More defensive code for when people remove sensors.
   
### [0.9.6] - Reload error fix
- In rare care case person was troubled by reload error, loosing all settings

### [0.9.5] - Auto card registration fix
- small fix for auto registration that would add another resource allocation on new install (should not be a big issue)
- Nearing our 1.0.0 release!!
  
### [0.9.3] - Automatic card install and registration
- Automatic card install and registration!
- No more need for separate card repository download and install (delete it!)
- Note: you need to delete your previous resource registry entry if you did it manually 
    
### [0.9.2] - Small fixes
- New port detection to include even more exotic corporate grade switch(es)
- IfHighSpeed can now correctly be used (is factor of 1.000.000 smaller in reporting then IfSpeed)
- Card updates:
  - Fix Mbps always displaying even when Gbps should be displayed
  - Small hidden setting trucate_text=false if you dont want the UI to shorten your port names

### [0.9.1] - Compatibility update
- More switches now supported in auto port detection
- Card updates:
  - System item boxes can be hidden or shown per item
  - Clicking ports shows HA more info for ports (much better)
- Integration and card in one is currently not possible according HA experts
  - will leave it in as it works for some..
  - For other: separate repository for card (see Readme and Discussion)   
- In case of issue see Discussion section of repository
- fix Mbps always displaying even when Gbps should be displayed

### [0.9.0] - Major update
- Refactoring, faster startup on HA restart.
- Async snmp engine creation (does not solve HA warning, needs pysnmp 7.1 update)
- Card is now part of integration (but requires 1 manual step to add resource, see readme)
- Card is now visible in HA Card picker!
- Small card updates (improve hoover with click, tigther grouping of ports, background option)
- Resolved issues #8, #13, #14
### Risk of Breaking
- could break existing hubs (if so delete hub, re-add and re-configure)
- the separate repository with only the pro card is obsolete, please remove first
  
### [0.8.3-pre] - Refactoring
- Cisco auto port detection fix
- card + integration in one repo
  
### [0.8.2] - Update on auto port detection mode
- Netgear 724T auto detect and unifi fixed 

### [0.8.1] - Card installed separately via HACS
- Improved auto port detection for unifi after example from users
- card install via hacs in progress. Already available via custom repositories (partach/switch-port-card-pro-card)

### [0.8.0] - Port color modes
- Added port color modes (Traffic heatmap, Link speed, Actual speed classification, Vlan)
- only card is updated
  
### [0.7.2] - Live on HACS
- available via HACS!
- de and nl language file added
- Updated Readme with HACS link
  
### [0.7.0] - Hacs compliant
- Brands compliance now done.
- Making port visuals in card configurable for end user
- Other smaller optimizations

### [0.6.0] - Added language file
- en.json now added. More will be added later
- Integration Configuration screen much more descriptive
- Small improvement on card visual

### [0.5.5] - Official Release
- Added custom OID (use any oid value you want in your card)
- Configurable name for custom OID in card
- Updated configuration possibilities of integration (and card)
- Configurable update time (20s per default)
  
### [0.5.4] - Extensions
- VLAN indication per port visual in the card
- Fully auto port detection (via SNMP)
- Title enable/disable per port section
- System information show / hide
### Fixed
- 32 bit counter wrap around issue for actual tx/rx speed
- Issue with auto port detection not updating config settings


### [0.5.3] - Improvements
- Not showing POE attributes when not configured (no oids available)
- First rudimentory version of auto (number of) ports detection ('interface'description part of port attribute) 
 
### [0.5.2] - live rx/tx support
- Based on lifetime rx/tx counter determine the current rx/tx speed per port
- Boolean setting in UI to turn on and off the real time rx/tx speeds display
- Updated card configuration with ha look and feel
- Updated device discovery mechanism and functioning 
### Fixed
- Bandwith calculation improvements
- device selection for card
### Documentation
- bringing README up to date

### [0.5.1] - Full Multi-Switch Support
- Fixed unique ID conflicts when using multiple switches
- Added proper device registry separation using entry_id
- Dynamic device naming (hostname → config title → IP)
- Real firmware and model in device info
- 100% future-proof multi-instance support
### Fixed
- Multi instance handling with unique IDs
### Documentation
- bringing README up to date
  
### [0.5.0] – 2025-11-29
### Features
- Almost HACS compliant release (brands only thing still missing)
- Initial functional working release of Switch Port Card Pro integration for Home Assistant.
- Full SNMP monitoring for switch ports, including status, speed, vlan, total bandwidth, POE and more.
- System sensors for CPU, memory, uptime, firmware and hostname.
- Configurable OIDs and port range via options flow.
- Support any SNMP-enabled switches.
- Companion Lovelace card for visual port status, cpu, mem and more. (needs to be released seperately)
### Fixed
- PYSNMP 7.1 compliant
- Hacssfest compliancy and validation rules
### Documentation
- bringing README up to date
