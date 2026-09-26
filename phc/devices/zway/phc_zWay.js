/*************************************************************************
* phc_zWay.js - z-Way server-side helper script
*
* Extended from THC's thc_zWay.js (Copyright (C) 2014 Andreas Drollinger),
* targeting z-Way 4.x only -- the tag-reader functions' 1.x/2.x/3.x paths
* are dropped.
**************************************************************************
* Standalone reference script: load it into the z-Way server's Automation
* folder to expose the functions below over its HTTP JS/Run interface. Not
* used by PHC's zway device module, which talks to the controller over the
* ZAutomation WebSocket API and needs nothing installed on the controller.
*************************************************************************/

/****************************************************\
  Function: Get_Virtual
     Returns a virtual device's current state.

  HTTP GET equivalence:
     http://192.168.1.21:8083/ZAutomation/api/v1/devices/DummyDevice_15

  Parameters:
     vDeviceId - Virtual device identifier

  Returns:
     Virtual device state, or "" if the device doesn't exist

  Examples:
     > http://192.168.1.21:8083/JS/Run/Get_Virtual("DummyDevice_15")
     > -> "open"

  See also:
     <Set_Virtual>
\****************************************************/

Get_Virtual = function (vDeviceId) {
  try {
    var vdev = this.controller.devices.get(vDeviceId);
    return vdev.get("metrics:level");
  } catch (err) {}
  return "";
};

/****************************************************\
  Function: Set_Virtual
     Sets a virtual device's state.

  HTTP GET equivalence:
     http://192.168.1.21:8083/ZAutomation/api/v1/devices/DummyDevice_15/command/open
     (or close, or whatever the device supports)

  Parameters:
     vDeviceId - Virtual device identifier
     State - New state

  Returns:
     Virtual device state after the write, or "" if the device doesn't exist

  Examples:
     > http://192.168.1.21:8083/JS/Run/Set_Virtual("DummyDevice_15","open")
     > -> "open"

  See also:
     <Get_Virtual>
\****************************************************/

Set_Virtual = function (vDeviceId, State) {
  try {
    var vdev = this.controller.devices.get(vDeviceId);
    vdev.set("metrics:level", State);
    return Get_Virtual(vDeviceId);
  } catch (err) {}
  return "";
};

/****************************************************\
  Function: Configure_TagReader
     Configures a tag reader. On each lock/unlock event:
     * Binds a SwitchBinary Set(1) to trigger an audible notification.
     * If vDeviceNotifierId names an existing virtual device (see
       Set_Virtual), sets its metrics:level to "close"/"open".
     * Otherwise, only the audible notification is configured.

  HTTP GET equivalence (to create the bindings):
     > http://192.168.1.21:8083/JS/Run/zway.devices[22].Alarm.data[6][5].status.bind( function() {zway.devices[22].SwitchBinary.Set(true); });
     > http://192.168.1.21:8083/JS/Run/zway.devices[22].Alarm.data[6][6].status.bind( function() {zway.devices[22].SwitchBinary.Set(true); });

  Parameters:
     Node - Device (node) number
     vDeviceNotifierId - Virtual device identifier to notify on lock/unlock (optional)

  Returns:
     Confirmation string

  Examples:
     > http://192.168.1.21:8083/JS/Run/Configure_TagReader(22, "DummyDevice_15")
     > -> "OK, configured tag reader 22"
\****************************************************/

Configure_TagReader = function (Node, vDeviceNotifierId) {
  if (typeof vDeviceNotifierId === "undefined") vDeviceNotifierId = null;

  var vdev = null;
  if (vDeviceNotifierId !== null) {
    try {
      vdev = this.controller.devices.get(vDeviceNotifierId);
    } catch (err) {}
  }

  zway.devices[Node].Alarm.data[6][5].status.bind(function () {
    debugPrint("TagReader " + Node + " event: Lock\n");
    zway.devices[Node].SwitchBinary.Set(true);
    if (vdev) vdev.set("metrics:level", "close");
  });
  zway.devices[Node].Alarm.data[6][6].status.bind(function () {
    debugPrint("TagReader " + Node + " event: Unlock\n");
    zway.devices[Node].SwitchBinary.Set(true);
    if (vdev) vdev.set("metrics:level", "open");
  });

  return "OK, configured tag reader " + Node;
};

/****************************************************\
  Function: Get_TagReader
     Returns the most recent tag-reader event and its timestamp.

  HTTP GET equivalence:
     http://192.168.1.21:8083/ZWaveAPI/Run/devices[22].instances[0].Alarm.data[6][5].status.updateTime
     http://192.168.1.21:8083/ZWaveAPI/Run/devices[22].instances[0].Alarm.data[6][6].status.updateTime
     http://192.168.1.21:8083/ZWaveAPI/Run/devices[22].instances[0].Alarm.data[7].status.updateTime
     http://192.168.1.21:8083/ZWaveAPI/Run/devices[22].instances[0].UserCode.data[0].updateTime

  Parameters:
     Node - Device (node) number

  Returns:
     [timestamp, "lock" | "unlock" | "tamper"], or
     [timestamp, "wrongcode", code] if the last event was a bad code, or
     ["error", ...] if none of the underlying reads succeeded

  Examples:
     > http://192.168.1.21:8083/JS/Run/Get_TagReader(22)
     > -> [1388853574,"lock"]
\****************************************************/

Get_TagReader = function (Node) {
  var LockTime = -1, UnLockTime = -1, TamperTime = -1;
  var WrongCodeTime = -1, WrongCodeValue = "";

  try {
    LockTime = zway.devices[Node].instances[0].Alarm.data[6][5].status.updateTime;
  } catch (err) {}
  try {
    UnLockTime = zway.devices[Node].instances[0].Alarm.data[6][6].status.updateTime;
  } catch (err) {}
  try {
    TamperTime = zway.devices[Node].instances[0].Alarm.data[7].status.updateTime;
  } catch (err) {}
  try {
    WrongCodeTime = zway.devices[Node].instances[0].UserCode.data[0].updateTime;
  } catch (err) {}
  try {
    WrongCodeValue = zway.devices[Node].instances[0].UserCode.data[0].code.value;
  } catch (err) {}

  var MaxTime = Math.max(LockTime, UnLockTime, TamperTime, WrongCodeTime, 0);
  if (MaxTime === LockTime) {
    return [MaxTime, "lock"];
  } else if (MaxTime === UnLockTime) {
    return [MaxTime, "unlock"];
  } else if (MaxTime === TamperTime) {
    return [MaxTime, "tamper"];
  } else if (MaxTime === WrongCodeTime) {
    return [MaxTime, "wrongcode", WrongCodeValue];
  }
  return ["error", LockTime, UnLockTime, TamperTime, WrongCodeTime, MaxTime];
};

/****************************************************\
  Function: TagReader_LearnLastCode
     Learn the tag reader's last entered code. The tag reader will
     afterwards accept the new code as a valid one.

     Code learning sequence:
     * Using the 'Home' or 'Away' button, enter a new code or use a new
       unknown RFID tag.
     * Run TagReader_LearnLastCode, providing a new UserId (code storage
       location).
     * Wake up the tag reader, e.g. by entering a valid or invalid code.
       It will receive from the controller the command to learn the new
       code.
     * Try the new code or RFID tag: the tag reader will now recognize it
       as valid.

  Parameters:
     Node - Device (node) number
     UserId - User identifier (code storage location)

  Returns:
     Information string on whether the code learning was successful

  Examples:
     > http://192.168.1.21:8083/JS/Run/TagReader_LearnLastCode(22, 2)
     > -> "OK, registered code 52,52,52,52,52,52,0,0,0,0"

  See also:
     <Configure_TagReader>, <TagReader_ResetCode>
\****************************************************/

// See: http://forum.z-wave.me/viewtopic.php?f=3419&t=20551

TagReader_LearnLastCode = function (Node, UserId) {
  if (typeof UserId === "undefined") {
    return "Call: TagReader_LearnLastCode(Node, UserId)";
  }

  var uc = zway.devices[Node].UserCode;
  if (uc.data[0] && uc.data[0].hasCode.value) {
    var code = uc.data[0].code.value;
    if (typeof code === "string") {
      uc.Set(UserId, code, 1);
    } else {
      uc.SetRaw(UserId, code, 1);
    }
    return "OK, registered code " + code;
  }
  return "No code could be registered";
};

/****************************************************\
  Function: TagReader_ResetCode
     Reset one or all codes a tag reader knows. If no UserId is given, all
     codes are reset; otherwise only the code assigned to that UserId.
     After running this command the tag reader needs to be woken up to
     receive the command to perform the reset.

  Parameters:
     Node - Device (node) number
     UserId - User identifier (code storage location, optional)

  Returns:
     -

  Examples:
     > http://192.168.1.21:8083/JS/Run/TagReader_ResetCode(22) -> resets all codes
     > http://192.168.1.21:8083/JS/Run/TagReader_ResetCode(22,3) -> resets the UserId specific code

  See also:
     <Configure_TagReader>, <TagReader_LearnLastCode>
\****************************************************/

TagReader_ResetCode = function (Node, UserId) {
  if (typeof Node === "undefined") {
    return "Call: TagReader_ResetCode(Node [, UserId])";
  }

  var uc = zway.devices[Node].UserCode;
  if (typeof UserId === "undefined") {
    uc.Set(0, "", 0); // Reset all codes
  } else {
    uc.Set(UserId, "", 0); // Reset the UserId-specific code
  }
};
