from django.db import models

# Create your models here.
class VEHICLE_LOGS(models.Model):
    id = models.AutoField(db_column='ID', primary_key=True)
    uid = models.CharField(db_column='UID', max_length=50, blank=True, null=True)
    rfids = models.CharField(db_column='RFIDS', max_length=500, blank=True, null=True)
    vehicle_img_path = models.CharField(db_column='VEHICLE_IMG_PATH', max_length=500, blank=True, null=True)
    sample_1_img_path = models.CharField(db_column='SAMPLE_1_IMG_PATH', max_length=500, blank=True, null=True)
    sample_2_img_path = models.CharField(db_column='SAMPLE_2_IMG_PATH', max_length=500, blank=True, null=True)
    sample_3_img_path = models.CharField(db_column='SAMPLE_3_IMG_PATH', max_length=500, blank=True, null=True)
    QR_code_path = models.CharField(db_column='QR_CODE_PATH', max_length=500, blank=True, null=True)
    status = models.CharField(db_column='STATUS', max_length=20, blank=True, null=True)
    error_message = models.CharField(db_column='ERROR_MESSAGE', max_length=500, blank=True, null=True)
    
    create_time = models.DateTimeField(db_column='CREATE_TIME', blank=True, null=True)
    update_time = models.DateTimeField(db_column='UPDATE_TIME', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'VEHICLE_LOGS'
        indexes = [
            models.Index(fields=["rfids"], name="vl_rfid_idx"),
            models.Index(fields=["create_time"], name="vl_create_time_idx"),
        ]

class VEHICLE_MASTER(models.Model):
    id = models.AutoField(db_column='ID', primary_key=True)
    rfid = models.CharField(db_column='RFID', max_length=100, blank=True, null=True)
    vehicle_number = models.CharField(db_column='VEHICLE_NUMBER', max_length=50, blank=True, null=True)
    vendor_code = models.CharField(db_column='VENDOR_CODE', max_length=50, blank=True, null=True)
    
    create_time = models.DateTimeField(db_column='CREATE_TIME', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'VEHICLE_MASTER'
        indexes = [
            models.Index(fields=["rfid"], name="vm_rfid_idx"),
            models.Index(fields=["vehicle_number"], name="vm_vehicle_number_idx"),
            models.Index(fields=["vendor_code"], name="vm_vendor_code_idx"),
            models.Index(fields=["create_time"], name="vm_create_time_idx"),
            models.Index(fields=["rfid", "vendor_code"], name="vm_rfid_vendor_code_idx"),
            models.Index(fields=["rfid", "vehicle_number"], name="vm_rfid_vehicle_number_idx"),
        ]

class VENDOR_MASTER(models.Model):
    id = models.AutoField(db_column='ID', primary_key=True)
    vendor_code = models.CharField(db_column='VENDOR_CODE', max_length=50, blank=True, null=True)
    vendor_name = models.CharField(db_column='VENDER_NAME', max_length=50, blank=True, null=True)
    bucket_no = models.CharField(db_column='BUCKET_NO', max_length=50, blank=True, null=True)
    
    create_time = models.DateTimeField(db_column='CREATE_TIME', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'VENDOR_MASTER'
        indexes = [
            models.Index(fields=["vendor_code"], name="vrm_vendor_code_idx"),
            models.Index(fields=["vendor_name"], name="vrm_vendor_name_idx"),
            models.Index(fields=["create_time"], name="vrm_create_time_idx"),
            models.Index(fields=["vendor_code", "vendor_name"], name="vrm_vendor_code_name_idx"),
        ]

class PLC_COMM(models.Model):
    id = models.AutoField(db_column='ID', primary_key=True)
    uid = models.CharField(db_column='UID', max_length=50)
    state = models.CharField(db_column='STATE', max_length=50)
    status = models.CharField(db_column='STATUS', max_length=20, blank=True, null=True)
    emergency = models.CharField(db_column='EMERGENCY', max_length=20, blank=True, null=True)
    auto_manual = models.CharField(db_column='AUTO_MANUAL', max_length=20, blank=True, null=True)
    # emergency_acknowledged = models.BooleanField(db_column='EMERGENCY_ACKNOWLEDGED', default=False, blank=True, null=True)
    # auto_manual_acknowledged = models.BooleanField(db_column='AUTO_MANUAL_ACKNOWLEDGED', default=False, blank=True, null=True)
    # user_approved_skip_cycles = models.BooleanField(db_column='USER_APPROVED_SKIP_CYCLES', default=False, blank=True, null=True)
    updated = models.DateTimeField(db_column='UPDATED', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'PLC_COMM'
        indexes = [
            models.Index(fields=["uid"], name="plc_uid_idx"),
            models.Index(fields=["state"], name="plc_state_idx"),
            models.Index(fields=["status"], name="plc_status_idx"),
            models.Index(fields=["emergency"], name="plc_emergency_idx"),
        ]

class HEALTH_STATUS(models.Model):
    id = models.AutoField(db_column='ID', primary_key=True)
    location = models.CharField(db_column='LOCATION', max_length=50)
    device_type = models.CharField(db_column='DEVICE_TYPE', max_length=10, blank=True, null=True)
    ip = models.CharField(db_column='IP', max_length=50, blank=True, null=True, unique=True)
    camera_serial_number = models.CharField(db_column='CAMERA_SERIAL_NUMBER', max_length=50, blank=True, null=True)
    status = models.CharField(db_column='STATUS', max_length=20, blank=True, null=True)
    last_ping = models.DateTimeField(db_column='LAST_PING', blank=True, null=True)
    top = models.PositiveSmallIntegerField(db_column='POS_TOP', blank=True, null=True)
    left = models.PositiveSmallIntegerField(db_column='POS_LEFT', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'HEALTH_STATUS'
        indexes = [
            models.Index(fields=["location"], name="health_location_idx"),
            models.Index(fields=["device_type"], name="health_type_idx"),
            models.Index(fields=["status"], name="health_status_idx"),
            models.Index(fields=["last_ping"], name="hs_time_idx"),
        ]