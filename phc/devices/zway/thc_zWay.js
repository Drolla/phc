/*************************************************************************
 * thc_zWay.js - zWay extension for PHC
 *
 * Small helper script exposing Get()/Set()/Configure_TagReader() (plus
 * their per-command-group Get_*/Set_* building blocks) over the zWay
 * HTTP interface (/JS/Run/...), so devices/zway/device.py can read/write
 * Z-Wave nodes with one combined request per poll instead of walking the
 * zway.devices[] tree itself over HTTP.
 *
 * Ported from THC (https://github.com/Drolla/thc), keeping the original
 * file name since it's just a copy of THC's own thc_zWay.js. Install
 * this file in the zWay server's automation folder; PHC loads it itself
 * on first use via executeFile("thc_zWay.js") (see
 * ZWayDevice._ensure_helper_loaded in devices/zway/device.py) -- PHC
 * never uploads the file's content, only triggers zWay to load a copy
 * already placed there.
 *************************************************************************/

var InvalidTimespan = {Battery: 21600, Multilevel: 1800};
var zWayRevision = zway.controller.data.softwareRevisionVersion.value;

// Get_Virtual(NI) -> virtual device state.
// Usage: http://192.168.1.21:8083/JS/Run/Get_Virtual("DummyDevice_bn_5")
//        -> 1
Get_Virtual = function(NI) {
	try {
		dev = this.controller.devices.get(NI);
		State = dev.get("metrics:level");

		switch (State) {
			case "on":
				State = 1;
				break;
			case "off":
				State = 0;
				break;
		}

		return (State);
	}
	catch (err) {}
	return "";
}

// Set_Virtual(NI, State) -> sets and returns the virtual device state.
// Usage: http://192.168.1.21:8083/JS/Run/Set_Virtual("DummyDevice_bn_5",1)
//        -> 1
Set_Virtual = function(NI, State) {
	switch (State) {
		case 0:
			State = "off";
			break;
		case 1:
			State = "on";
			break;
	}

	try {
		dev = this.controller.devices.get(NI);
		dev.set("metrics:level", State)
		return (Get_Virtual(NI));
	}
	catch (err) {}
	return "";
}

// Get_IndexArray(NI) splits a "device.instance.datarecord" identifier into
// its three numeric parts (missing parts default to 0). Used by
// ZWayDevice._ensure_helper_loaded as a marker call to check that this
// file is loaded on the zWay server.
// Usage: http://192.168.1.21:8083/JS/Run/Get_IndexArray(8.1)
//        -> [8,1,0]
//        http://192.168.1.21:8083/JS/Run/Get_IndexArray(7)
//        -> [7,0,0]
//        http://192.168.1.21:8083/JS/Run/Get_IndexArray("6.1.2")
//        -> [6,1,2]
Get_IndexArray = function(NI) {
	var ThreeVal = String(NI).split(".");
	ThreeVal[0] = parseInt(ThreeVal[0]);
	ThreeVal[1] = (ThreeVal.length > 1 ? parseInt(ThreeVal[1]) : 0);
	ThreeVal[2] = (ThreeVal.length > 2 ? parseInt(ThreeVal[2]) : 0);
	return ThreeVal;
}

// Sleep(NI, state) -> puts a battery device's wakeup instance to sleep.
// Usage: http://192.168.1.21:8083/JS/Run/Sleep([2,5,12,15])
//        -> 1
Sleep = function(NI, state) {
	var IndexArray = Get_IndexArray(NI);
	try {
		zway.devices[IndexArray[0]].instances[0].Wakeup.Sleep();
		return 1;
	}
	catch (err) {}
	return "";
};

// Set_SwitchBinary(NI, state)
// Usage: http://192.168.1.21:8083/JS/Run/Set_SwitchBinary(7.1, 0)
//        -> 0
Set_SwitchBinary = function(NI, state) {
	var IndexArray = Get_IndexArray(NI);
	try {
		zway.devices[IndexArray[0]].instances[IndexArray[1]].SwitchBinary.Set(state == 0 ? 0 : 255);
	}
	catch (err) {}
	return Get_SwitchBinary(NI);
}

// Get_SwitchBinary(NI)
// Usage: http://192.168.1.21:8083/JS/Run/Get_SwitchBinary(8.0)
//        -> 1
Get_SwitchBinary = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	try {
		return (zway.devices[IndexArray[0]].instances[IndexArray[1]].SwitchBinary.data.level.value == 0) ? 0 : 1;
	}
	catch (err) {}
	return "";
}

// Set_SwitchMultiBinary(NI, state) -> sets a range of binary switch
// instances (IndexArray[1]..IndexArray[2]) from the bits of `state`.
// Usage: http://192.168.1.21:8083/JS/Run/Set_SwitchMultiBinary("33.1.2", 3)
//        -> 3
Set_SwitchMultiBinary = function(NI, state) {
	state = Math.round(state);
	var IndexArray = Get_IndexArray(NI);
	try {
		for (var Idx = IndexArray[1]; Idx <= IndexArray[2]; Idx++) {
			zway.devices[IndexArray[0]].instances[Idx].SwitchBinary.Set((state & 1) == 0 ? 0 : 255);
			state = state >>> 1;
		}
	}
	catch (err) {}
	return Get_SwitchMultiBinary(NI);
}

// Get_SwitchMultiBinary(NI) -> reassembles a range of binary switch
// instances (IndexArray[1]..IndexArray[2]) into one bitfield.
// Usage: http://192.168.1.21:8083/JS/Run/Get_SwitchMultiBinary("33.1.2")
//        -> 3
Get_SwitchMultiBinary = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	var State = 0;
	try {
		for (var Idx = IndexArray[2]; Idx >= IndexArray[1]; Idx--) {
			State = (State * 2) | (zway.devices[IndexArray[0]].instances[Idx].SwitchBinary.data.level.value == 0 ? 0 : 1);
		}
	}
	catch (err) {
		State = ""
	}
	return State;
}

// Get_SensorBinary(NI)
// Usage: http://192.168.1.21:8083/JS/Run/Get_SensorBinary(2)
//        -> 1
Get_SensorBinary = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	try {
		return (zway.devices[IndexArray[0]].instances[0].SensorBinary.data[1].level.value == 0) ? 1 : 0;
	}
	catch (err) {
		return "";
	}
}

// Set_SwitchMultilevel(NI, level) -> level is a float between 0.0 and 1.0.
// Usage: http://192.168.1.21:8083/JS/Run/Set_SwitchMultilevel(12.0, 0.25)
//        -> 0.25
Set_SwitchMultilevel = function(NI, level) {
	var IndexArray = Get_IndexArray(NI);
	try {
		zway.devices[IndexArray[0]].instances[IndexArray[1]].SwitchMultilevel.Set(Math.round(99 * level));
	}
	catch (err) {}
	return Get_SwitchMultilevel(NI);
}

// Get_SwitchMultilevel(NI) -> level as a float between 0.0 and 1.0.
// Usage: http://192.168.1.21:8083/JS/Run/Get_SwitchMultilevel(12.0)
//        -> 0.50
Get_SwitchMultilevel = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	try {
		return Math.round(101.01 * zway.devices[IndexArray[0]].instances[IndexArray[1]].SwitchMultilevel.data.level.value) / 10000;
	}
	catch (err) {
		return "";
	}
}

// Configure_TagReader(NI) -> binds a tag reader's lock/unlock alarm event
// to an audible-notification SwitchBinary.Set(true) on the same device.
// One-time setup call, issued the first time a TagReader device is polled
// (see ZWayDevice._ensure_tag_readers_configured).
// Usage: http://192.168.1.21:8083/JS/Run/Configure_TagReader(22)
//        -> null
Configure_TagReader = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	if (zWayRevision.substr(0, 2) == "v1.") { // z-Way version 1.x
		zway.devices[IndexArray[0]].Alarm.data[6][5].status.bind(function() {
			zway.devices[IndexArray[0]].SwitchBinary.Set(true);
		});
		zway.devices[IndexArray[0]].Alarm.data[6][6].status.bind(function() {
			zway.devices[IndexArray[0]].SwitchBinary.Set(true);
		});
	} else { // z-Way version 2.x, ...
		zway.devices[IndexArray[0]].Alarm.data[6].event.bind(function() {
			zway.devices[IndexArray[0]].SwitchBinary.Set(true);
		});
	}
}

// Get_TagReader(NI) -> [time, "lock"|"unlock"|"tamper"|"wrongcode"[, code]]
// for the most recent tag reader event.
// Usage: http://192.168.1.21:8083/JS/Run/Get_TagReader(22)
//        -> [1388853574,"lock"]
Get_TagReader = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	var LastEvent = "";
	var LockTime = -1, UnLockTime = -1, TamperTime = -1, WrongCodeTime = -1, MaxTime = -1, WrongCodeValue = "";

	if (zWayRevision.substr(0, 2) == "v1.") { // z-Way version 1.x
		try { LockTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[6][5].status.updateTime; } catch (err) {}
		try { UnLockTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[6][6].status.updateTime; } catch (err) {}
		try { TamperTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[7][3].status.updateTime; } catch (err) {}
	} else { // z-Way version 2.x, ...
		try {
			if (zway.devices[IndexArray[0]].instances[0].Alarm.data[6].event.value == 5) {
				LockTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[6].event.updateTime;
			}
		} catch (err) {}
		try {
			if (zway.devices[IndexArray[0]].instances[0].Alarm.data[6].event.value == 6) {
				UnLockTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[6].event.updateTime;
			}
		} catch (err) {}
		try {
			if (zway.devices[IndexArray[0]].instances[0].Alarm.data[7].event.value == 3) {
				TamperTime = zway.devices[IndexArray[0]].instances[0].Alarm.data[7].event.updateTime;
			}
		} catch (err) {}
	}
	try { WrongCodeTime = zway.devices[IndexArray[0]].instances[0].UserCode.data[0].updateTime; } catch (err) {}
	try { WrongCodeValue = zway.devices[IndexArray[0]].instances[0].UserCode.data[0].code.value; } catch (err) {}

	MaxTime = Math.max(LockTime, UnLockTime, TamperTime, WrongCodeTime, 0);
	if (MaxTime == LockTime) {
		return [MaxTime, "lock"];
	}
	else if (MaxTime == UnLockTime) {
		return [MaxTime, "unlock"];
	}
	else if (MaxTime == TamperTime) {
		return [MaxTime, "tamper"];
	}
	else if (MaxTime == WrongCodeTime) {
		return [MaxTime, "wrongcode", WrongCodeValue];
	}
	return "";
};

// TagReader_LearnLastCode(NI, UserId) -> registers the last entered code
// (or scanned RFID tag) as valid, stored at slot UserId. Follow-up manual
// step required afterwards: wake the tag reader (enter any code) so it
// receives the learned code.
// Usage: http://192.168.1.21:8083/JS/Run/TagReader_LearnLastCode(22, 2)
//        -> OK, registered code 52,52,52,52,52,52,0,0,0,0
// See: http://forum.z-wave.me/viewtopic.php?f=3419&t=20551
TagReader_LearnLastCode = function(NI, UserId) {
	if (typeof UserId == "undefined")
		return "Call: TagReader_LearnLastCode(NI, UserId)";
	var IndexArray = Get_IndexArray(NI);
	var uc = zway.devices[IndexArray[0]].UserCode;
	if (uc.data[0] && uc.data[0].hasCode.value) {
		var code = uc.data[0].code.value;
		if (typeof code === "string") {
			uc.Set(UserId, code, 1);
		} else {
			uc.SetRaw(UserId, code, 1);
		}
		return "OK, registered code " + code;
	} else {
		return "No code could be registered";
	}
}

// TagReader_ResetCode(NI[, UserId]) -> resets one code slot, or all slots
// if UserId is omitted. Follow-up manual step required afterwards: wake
// the tag reader so it receives the reset.
// Usage: http://192.168.1.21:8083/JS/Run/TagReader_ResetCode(22)
//          -> resets all codes
//        http://192.168.1.21:8083/JS/Run/TagReader_ResetCode(22,3)
//          -> resets the UserId specific code
TagReader_ResetCode = function(NI, UserId) {
	if (typeof NI == "undefined")
		return "Call: TagReader_ResetCode(NI [, UserId])";
	var IndexArray = Get_IndexArray(NI);
	var uc = zway.devices[IndexArray[0]].UserCode;
	if (typeof UserId == "undefined")
		uc.Set(0, '', 0); // Reset all codes
	else
		uc.Set(UserId, '', 0); // Reset the UserId specific code
}

// Get_Battery(NI) -> battery level (0-100), or "" if stale/unavailable.
// A reading older than InvalidTimespan.Battery seconds is treated as
// unavailable (the node hasn't woken up recently enough to report).
// Usage: http://192.168.1.21:8083/JS/Run/Get_Battery(22)
//        -> 67
Get_Battery = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	var CurrentTime = Math.round(Date.now() / 1000);
	try {
		zway.devices[IndexArray[0]].instances[0].Battery.Get();
		var UpdateTime = zway.devices[IndexArray[0]].instances[0].Battery.data.last.updateTime;
		if (CurrentTime <= UpdateTime + InvalidTimespan["Battery"]) {
			var Level = zway.devices[IndexArray[0]].instances[0].Battery.data.last.value;
			return (Level > 100 ? 0 : Level); // An empty battery is reported as 255
		}
		else {
			return "";
		}
	}
	catch (err) {
		return "";
	}
}

// Get_SensorMultilevel(NI) -> reading, or "" if stale/unavailable. A
// reading older than InvalidTimespan.Multilevel seconds is treated as
// unavailable.
// Usage: http://192.168.1.21:8083/JS/Run/Get_SensorMultilevel("5.0.1")
//        -> 77
Get_SensorMultilevel = function(NI) {
	var IndexArray = Get_IndexArray(NI);
	var CurrentTime = Math.round(Date.now() / 1000);
	try {
		zway.devices[IndexArray[0]].instances[IndexArray[1]].SensorMultilevel.Get();
		var UpdateTime = zway.devices[IndexArray[0]].instances[IndexArray[1]].SensorMultilevel.data[IndexArray[2]].val.updateTime;
		if (CurrentTime <= UpdateTime + InvalidTimespan["Multilevel"]) {
			return zway.devices[IndexArray[0]].instances[IndexArray[1]].SensorMultilevel.data[IndexArray[2]].val.value;
		}
	}
	catch (err) {}
	return "";
}

// Get(DeviceList) -> one value per [command_group, address] entry in
// DeviceList, dispatched to the matching Get_* function above. The
// combined-read entry point ZWayDevice._download() calls once per poll
// for every currently-registered identifier on a controller.
// Usage: http://192.168.1.21:8083/JS/Run/Get([["Control","Surveillance"],["SwitchBinary",7.1],["SensorBinary",2],["TagReader",22],["Battery",22],["SensorMultilevel","5.0.1"]])
//        -> [0,0,1,[1407694169,"unlock"],33,17.7]
//        http://192.168.1.21:8083/JS/Run/Get([["Virtual","DummyDevice_bn_5"]])
//        -> [0]
Get = function(DeviceList) {
	var ResultArray = new Array();
	var CurrentTime = Math.round(Date.now() / 1000);
	for (var i = 0; i < DeviceList.length; i++) {
		var Value = "";
		try {
			var Device = DeviceList[i];

			switch (Device[0]) {
				case "Virtual":
					Value = Get_Virtual(Device[1]);
					break;
				case "SwitchBinary":
					Value = Get_SwitchBinary(Device[1]);
					break;
				case "SwitchMultiBinary":
					Value = Get_SwitchMultiBinary(Device[1]);
					break;
				case "SensorBinary":
					Value = Get_SensorBinary(Device[1]);
					break;
				case "TagReader":
					Value = Get_TagReader(Device[1]);
					break;
				case "Battery":
					Value = Get_Battery(Device[1]);
					break;
				case "SensorMultilevel":
					Value = Get_SensorMultilevel(Device[1]);
					break;
				case "SwitchMultilevel":
					Value = Get_SwitchMultilevel(Device[1]);
					break;
				default:
					break;
			}
		}
		catch (err) {}
		ResultArray[i] = Value;
	}
	return JSON.stringify(ResultArray); // Stringify, required for z-Way 2.x
}

// Set(DeviceList, State) -> writes State to every [command_group, address]
// entry in DeviceList, dispatched to the matching Set_* function above.
// The one-shot write entry point ZWayDevice.transmit_async() calls per
// endpoint write.
// Usage: http://192.168.1.21:8083/JS/Run/Set([["Virtual","DummyDevice_bn_5"]],1)
//        -> [1]
//        http://192.168.1.21:8083/JS/Run/Set([["SwitchBinary",20.1]],1)
//        -> [1]
Set = function(DeviceList, State) {
	var ResultArray = new Array();
	var CurrentTime = Math.round(Date.now() / 1000);
	for (var i = 0; i < DeviceList.length; i++) {
		var Value = "";
		try {
			var Device = DeviceList[i];

			switch (Device[0]) {
				case "Virtual":
					Value = Set_Virtual(Device[1], State);
					break;
				case "SwitchBinary":
					Value = Set_SwitchBinary(Device[1], State);
					break;
				case "SwitchMultiBinary":
					Value = Set_SwitchMultiBinary(Device[1], State);
					break;
				case "SwitchMultilevel":
					Value = Set_SwitchMultilevel(Device[1], State);
					break;
				default:
					break;
			}
		}
		catch (err) {}
		ResultArray[i] = Value;
	}
	return JSON.stringify(ResultArray);
}
