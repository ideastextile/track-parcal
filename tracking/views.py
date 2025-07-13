from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import login, logout
from django.shortcuts import get_object_or_404
from django.utils import timezone
from .models import User, Driver, Parcel, TrackingEvent, Job, Notification, AboutSection
from .serializers import (
    UserSerializer, LoginSerializer, DriverSerializer, ParcelSerializer,
    ParcelBookingSerializer, JobSerializer, NotificationSerializer,
    ParcelTrackingSerializer, DriverLocationUpdateSerializer,
    DeliveryCompletionSerializer, TrackingEventSerializer
)

#website views
 

def about_page(request):
    about = AboutSection.objects.first()
    return render(request, 'tracking/about.html', {'about': about})



# Authentication Views
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.validated_data['user']
            login(request, user)
            return Response({
                'message': 'Login successful',
                'user': UserSerializer(user).data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    def post(self, request):
        logout(request)
        return Response({'message': 'Logout successful'})


# Customer Views
class ParcelBookingView(generics.CreateAPIView):
    serializer_class = ParcelBookingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        parcel = serializer.save()
        # Create initial tracking event
        TrackingEvent.objects.create(
            parcel=parcel,
            status_update='Order placed',
            notes='Parcel booking confirmed',
            created_by=self.request.user
        )
        # Create notification for customer
        Notification.objects.create(
            user=parcel.customer,
            title='Parcel Booked Successfully',
            message=f'Your parcel with tracking number {parcel.tracking_number} has been booked.',
            parcel=parcel
        )


class CustomerParcelsView(generics.ListAPIView):
    serializer_class = ParcelSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Parcel.objects.filter(customer=self.request.user)


class ParcelDetailView(generics.RetrieveAPIView):
    serializer_class = ParcelSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = 'tracking_number'

    def get_queryset(self):
        user = self.request.user
        if user.user_type == 'customer':
            return Parcel.objects.filter(customer=user, can_customer_track=True)
        elif user.user_type in ['controller', 'driver']:
            return Parcel.objects.all()
        return Parcel.objects.none()


# Public Tracking View
class PublicTrackingView(generics.RetrieveAPIView):
    serializer_class = ParcelTrackingSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'tracking_number'
    queryset = Parcel.objects.all()


    def get_queryset(self):
        return Parcel.objects.all()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()

        if not instance.can_customer_track:
            return Response({
                'error': 'Tracking is not available for this parcel yet.'
            }, status=status.HTTP_403_FORBIDDEN)

        serializer = self.get_serializer(instance)
        return Response(serializer.data)

# Controller Views
class AllParcelsView(generics.ListAPIView):
    serializer_class = ParcelSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if self.request.user.user_type == 'controller':
            return Parcel.objects.all()
        return Parcel.objects.none()


class AllDriversView(generics.ListAPIView):
    serializer_class = DriverSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if self.request.user.user_type == 'controller':
            return Driver.objects.all()
        return Driver.objects.none()


class AssignDriverView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, parcel_id):
        if request.user.user_type != 'controller':
            return Response({'error': 'Only controllers can assign drivers'}, 
                          status=status.HTTP_403_FORBIDDEN)

        parcel = get_object_or_404(Parcel, id=parcel_id)
        driver_id = request.data.get('driver_id')
        job_type = request.data.get('job_type', 'pickup')

        if not driver_id:
            return Response({'error': 'Driver ID is required'}, 
                          status=status.HTTP_400_BAD_REQUEST)

        driver = get_object_or_404(Driver, pk=driver_id)

        # Create job
        job = Job.objects.create(
            parcel=parcel,
            driver=driver,
            job_type=job_type
        )

        # Update parcel status
        if job_type == 'pickup':
            parcel.status = 'awaiting_pickup'
        else:
            parcel.status = 'out_for_delivery'
            parcel.can_customer_track = True  # ✅ Allow customer to track now

        parcel.current_driver = driver
        parcel.save()

        # Create tracking event
        TrackingEvent.objects.create(
            parcel=parcel,
            status_update=f'Assigned to driver for {job_type}',
            notes=f'Driver {driver.user.username} assigned for {job_type}',
            created_by=request.user
        )

        # Create notification for driver
        Notification.objects.create(
            user=driver.user,
            title=f'New {job_type.title()} Job Assigned',
            message=f'You have been assigned a {job_type} job for parcel {parcel.tracking_number}',
            parcel=parcel
        )

        return Response({'message': 'Driver assigned successfully', 'job_id': job.id})


# Driver Views
class DriverJobsView(generics.ListAPIView):
    serializer_class = JobSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if self.request.user.user_type == 'driver':
            try:
                driver = Driver.objects.get(user=self.request.user)
                return Job.objects.filter(driver=driver)
            except Driver.DoesNotExist:
                return Job.objects.none()
        return Job.objects.none()


class AcceptJobView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, job_id):
        if request.user.user_type != 'driver':
            return Response({'error': 'Only drivers can accept jobs'}, 
                          status=status.HTTP_403_FORBIDDEN)

        job = get_object_or_404(Job, id=job_id)
        
        if job.driver.user != request.user:
            return Response({'error': 'You can only accept your own jobs'}, 
                          status=status.HTTP_403_FORBIDDEN)

        job.status = 'accepted'
        job.accepted_at = timezone.now()
        job.save()

        # Create tracking event
        TrackingEvent.objects.create(
            parcel=job.parcel,
            status_update=f'Driver accepted {job.job_type} job',
            notes=f'Driver {request.user.username} accepted the job',
            created_by=request.user
        )

        return Response({'message': 'Job accepted successfully'})


class ScanParcelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, job_id):
        if request.user.user_type != 'driver':
            return Response({'error': 'Only drivers can scan parcels'}, 
                          status=status.HTTP_403_FORBIDDEN)

        job = get_object_or_404(Job, id=job_id)
        
        if job.driver.user != request.user:
            return Response({'error': 'You can only scan parcels for your own jobs'}, 
                          status=status.HTTP_403_FORBIDDEN)

        # Update job status
        job.status = 'en_route'
        job.save()

        # Update parcel status
        if job.job_type == 'pickup':
            job.parcel.status = 'collected'
            status_message = 'Parcel collected and scanned'
        else:
            job.parcel.status = 'out_for_delivery'
            job.parcel.can_customer_track = True  # ✅ Allow customer to track now
            status_message = 'Parcel scanned for delivery'
        
        job.parcel.save()

        

        # Create tracking event
        TrackingEvent.objects.create(
            parcel=job.parcel,
            status_update=status_message,
            notes=f'Parcel scanned by driver {request.user.username}',
            created_by=request.user
        )

        # Create notification for customer
        Notification.objects.create(
            user=job.parcel.customer,
            title='Parcel Status Update',
            message=f'Your parcel {job.parcel.tracking_number} has been {status_message.lower()}',
            parcel=job.parcel
        )

        return Response({'message': 'Parcel scanned successfully'})


class UpdateLocationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.user_type != 'driver':
            return Response({'error': 'Only drivers can update location'}, 
                          status=status.HTTP_403_FORBIDDEN)

        serializer = DriverLocationUpdateSerializer(data=request.data)
        if serializer.is_valid():
            try:
                driver = Driver.objects.get(user=request.user)
                driver.current_latitude = serializer.validated_data['latitude']
                driver.current_longitude = serializer.validated_data['longitude']
                driver.save()
                return Response({'message': 'Location updated successfully'})
            except Driver.DoesNotExist:
                return Response({'error': 'Driver profile not found'}, 
                              status=status.HTTP_404_NOT_FOUND)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CompleteDeliveryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, job_id):
        if request.user.user_type != 'driver':
            return Response({'error': 'Only drivers can complete deliveries'}, 
                          status=status.HTTP_403_FORBIDDEN)

        job = get_object_or_404(Job, id=job_id)
        
        if job.driver.user != request.user:
            return Response({'error': 'You can only complete your own jobs'}, 
                          status=status.HTTP_403_FORBIDDEN)

        serializer = DeliveryCompletionSerializer(data=request.data)
        if serializer.is_valid():
            # Update job
            job.status = 'completed'
            job.completed_at = timezone.now()
            job.notes = serializer.validated_data.get('notes', '')
            job.save()

            # Update parcel
            job.parcel.status = 'delivered'
            job.parcel.save()

            # Create tracking event with proof
            tracking_event = TrackingEvent.objects.create(
                parcel=job.parcel,
                status_update='Delivered successfully',
                notes=serializer.validated_data.get('notes', 'Package delivered'),
                created_by=request.user
            )

            # Add delivery proof if provided
            if 'delivery_image' in serializer.validated_data:
                tracking_event.image = serializer.validated_data['delivery_image']
            if 'signature' in serializer.validated_data:
                tracking_event.signature = serializer.validated_data['signature']
            tracking_event.save()

            # Create notification for customer
            Notification.objects.create(
                user=job.parcel.customer,
                title='Parcel Delivered',
                message=f'Your parcel {job.parcel.tracking_number} has been delivered successfully',
                parcel=job.parcel
            )

            return Response({'message': 'Delivery completed successfully'})
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Notification Views
class NotificationsView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)


class MarkNotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, notification_id):
        notification = get_object_or_404(Notification, id=notification_id, user=request.user)
        notification.is_read = True
        notification.save()
        return Response({'message': 'Notification marked as read'})



# Web Interface Views
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages


def home_view(request):
    return render(request, 'tracking/home.html')

def home1_view(request):
    return render(request, 'tracking/index.html')

def services_page(request):
    return render(request, 'tracking/service.html')

def about_view(request):
    about = AboutSection.objects.first()
    return render(request, 'tracking/about.html', {'about': about})


def login_page(request):
    if request.user.is_authenticated:
        return redirect('home')
    return render(request, 'tracking/login.html')


def register_page(request):
    if request.user.is_authenticated:
        return redirect('home')
    return render(request, 'tracking/register.html')


def logout_page(request):
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('home')


def track_parcel_page(request):
    return render(request, 'tracking/track_parcel.html')


@login_required
def book_parcel_page(request):
    if request.user.user_type != 'customer':
        messages.error(request, 'Only customers can book parcels.')
        return redirect('home')
    return render(request, 'tracking/book_parcel.html')


@login_required
def my_parcels_page(request):
    if request.user.user_type != 'customer':
        messages.error(request, 'Access denied.')
        return redirect('home')
    return render(request, 'tracking/my_parcels.html')


# @login_required
# def admin_dashboard_page(request):
#     if request.user.user_type != 'controller':
#         messages.error(request, 'Access denied.')
#         return redirect('home')
#     return render(request, 'tracking/admin_dashboard.html')


@login_required
def driver_dashboard_page(request):
    if request.user.user_type != 'driver':
        messages.error(request, 'Access denied.')
        return redirect('home')
    return render(request, 'tracking/driver_dashboard.html')


@login_required
def profile_page(request):
    return render(request, 'tracking/profile.html')


# @login_required
# def admin_dashboard_page(request):
#     if request.user.user_type != 'controller':
#         messages.error(request, 'Access denied.')
#         return redirect('home')

#     # 🟢 Send all parcels to the template
#     parcels = Parcel.objects.select_related('customer', 'current_driver__user').all()

#     return render(request, 'tracking/admin_dashboard.html', {
#         'parcels': parcels
#     })

@login_required
def admin_dashboard_page(request):
    if request.user.user_type != 'controller':
        messages.error(request, 'Access denied.')
        return redirect('home')

    from .models import Parcel, Job

    total_parcels = Parcel.objects.count()
    parcels = Parcel.objects.select_related('customer', 'current_driver__user').all()
    pending_pickup = Parcel.objects.filter(status='awaiting_pickup').count()
    in_transit = Parcel.objects.filter(status='out_for_delivery').count()
    delivered = Parcel.objects.filter(status='delivered').count()
    total_jobs = Job.objects.count()
    jobs = Job.objects.select_related('driver', 'parcel').all()

    return render(request, 'tracking/admin_dashboard.html', {
        'total_parcels': total_parcels,
        'pending_pickup': pending_pickup,
        'in_transit': in_transit,
        'delivered': delivered,
        'total_jobs': total_jobs,
        'parcels': parcels,
        'jobs': jobs
    })
