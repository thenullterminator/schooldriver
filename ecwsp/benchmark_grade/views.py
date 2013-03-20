#   Copyright 2011 Burke Software and Consulting LLC
#   Author: John Milner <john@tmoj.net>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#   MA 02110-1301, USA.

from django.shortcuts import render_to_response, get_object_or_404
from django.contrib.auth.decorators import user_passes_test, login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect
from django.db.models import Q, Max, Count
from django.db import transaction
from django.template import RequestContext
from django.core.urlresolvers import reverse

from ecwsp.sis.models import SchoolYear, Student, Faculty
#from ecwsp.sis.uno_report import *
from ecwsp.schedule.models import Course, MarkingPeriod
#from ecwsp.schedule.forms import 
from ecwsp.grades.forms import GradeUpload
#from ecwsp.administration.models import *
from ecwsp.benchmark_grade.models import Category, Mark, Aggregate, Item, Demonstration, CalculationRule, AggregateTask
from ecwsp.benchmark_grade.forms import BenchmarkGradeVerifyForm, GradebookFilterForm, ItemForm, DemonstrationForm, FillAllForm
from ecwsp.benchmarks.models import Benchmark
from ecwsp.benchmark_grade.utility import gradebook_get_average, gradebook_get_average_and_pk, gradebook_recalculate_on_item_change, gradebook_recalculate_on_mark_change
from ecwsp.benchmark_grade.utility import benchmark_find_calculation_rule
from ecwsp.benchmark_grade.tasks import benchmark_aggregate_task

from decimal import Decimal
import logging
import json
import datetime

def get_teacher_courses(username):
    """ Utility function that returns courses a given teacher may access """
    try:
        teacher = Faculty.objects.get(username=username)
        teacher_courses = Course.objects.filter(
            graded=True,
            marking_period__school_year__active_year=True,
        ).filter(Q(teacher=teacher) | Q(secondary_teachers=teacher)).distinct()
    except Faculty.DoesNotExist:
        teacher_courses = None
    return teacher_courses

#@user_passes_test(lambda u: u.groups.filter(Q(name='teacher') | Q(name="registrar")).count() > 0 or u.is_superuser, login_url='/')
@user_passes_test(lambda u: u.groups.filter(name="registrar").count() > 0 or u.is_superuser, login_url='/')
def benchmark_grade_upload(request, id):
    """ Grades can only be entered/changed by spreadsheet upload. """
    course = Course.objects.get(id=id)
    message = ''
    mps = ()
    
    available_mps = course.marking_period.filter(Q(active=True) | Q(start_date__lt=datetime.date.today))
    show_descriptions = True
    if request.method == 'POST':
        if 'upload' in request.POST:
            import_form = GradeUpload(request.POST, request.FILES)
            verify_form = BenchmarkGradeVerifyForm() 
            if import_form.is_valid():
                from ecwsp.benchmark_grade.importer import BenchmarkGradeImporter
                importer = BenchmarkGradeImporter(request.FILES['file'], request.user)
                message = importer.import_grades(course, import_form.cleaned_data['marking_period'])
        if 'verify' in request.POST:
            verify_form = BenchmarkGradeVerifyForm(request.POST)
            verify_form.fields['marking_periods'].queryset = available_mps 
            import_form = GradeUpload()
            if verify_form.is_valid():
                ''' basically the same as student_grade, except is per-student instead of per-course '''
                mps = MarkingPeriod.objects.filter(id__in=verify_form.cleaned_data['marking_periods'])
                if verify_form.cleaned_data['all_students']:
                    students = course.get_enrolled_students()
                else:
                    students = course.get_enrolled_students().filter(id__in=verify_form.cleaned_data['students'])
                categories = Category.objects.filter(item__course=course).distinct()
                for mp in mps:
                    mp.students = students.all() # must have all() to make a copy; loses all optimization gains
                    for student in mp.students:
                        student.categories = categories.all()
                        for category in student.categories:
                            category.marks = Mark.objects.filter(student=student, item__course=course, item__category=category,
                                                                 item__marking_period=mp).order_by('-item__date', 'item__name',
                                                                                                   'description')
                            if not verify_form.cleaned_data['all_demonstrations']:
                                category.marks = category.marks.filter(Q(description='Session') | Q(description=''))
                                # If all_demonstrations aren't shown, "Session" is assumed; description is unnecessary
                                show_descriptions = False
                            try:
                                agg = Aggregate.objects.get(student=student, course=course,
                                                            category=category, marking_period=mp)
                                category.average = agg.cached_value
                            except:
                                category.average = None
    else:
        import_form = GradeUpload()
        verify_form = BenchmarkGradeVerifyForm()
    verify_form.fields['marking_periods'].queryset = available_mps
    verify_form.fields['marking_periods'].initial = verify_form.fields['marking_periods'].queryset
    
    return render_to_response('benchmark_grade/upload.html', {
        'request': request,
        'course': course,
        'import_form': import_form,
        'verify_form': verify_form.as_p(),
        'message': message,
        'mps': mps,
        'show_descriptions': show_descriptions
    }, RequestContext(request, {}),)


@staff_member_required
def gradebook(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    teacher_courses = get_teacher_courses(request.user.username)
    if not request.user.is_superuser and not request.user.groups.filter(name='registrar').count() and \
    (teacher_courses is None or course not in teacher_courses):
        messages.add_message(request, messages.ERROR,
            'You do not have access to the gradebook for ' + course.fullname + '.')
        return HttpResponseRedirect(reverse('admin:index'))

    # lots of stuff will fail unceremoniously if there are no MPs assigned
    if not course.marking_period.count():
        messages.add_message(request, messages.ERROR,
            'The gradebook cannot be opened because there are no marking periods assigned to the course ' +
            course.fullname + '.')
        return HttpResponseRedirect(reverse('admin:index'))

    students = Student.objects.filter(inactive=False,course=course)
    #students = Student.objects.filter(course=course)
    items = Item.objects.filter(course=course)
    filtered = False

    if request.GET:
        filter_form = GradebookFilterForm(request.GET)
        filter_form.update_querysets(course)
        if filter_form.is_valid():
            for filter_key, filter_value in filter_form.cleaned_data.iteritems():
                if filter_value is not None:
                    try:
                        if not len(filter_value):
                            continue
                    except TypeError:
                        # not everything has a len
                        pass
                    if filter_key == 'cohort': 
                        students = students.filter(cohorts=filter_value)
                    if filter_key == 'marking_period':
                        items = items.filter(marking_period=filter_value)
                    if filter_key == 'benchmark':
                        items = items.filter(benchmark__in=filter_value)
                    if filter_key == 'category':
                        items = items.filter(category=filter_value)
                    if filter_key == 'assignment_type':
                        items = items.filter(assignment_type=filter_value)
                    if filter_key == 'name':
                        items = items.filter(name__icontains=filter_value)
                    if filter_key == 'date_begin':
                        items = items.filter(date__gt=filter_value)
                    if filter_key == 'date_end':
                        items = items.filter(date__lt=filter_value)
                    filtered = True
    else:
        # show only the active marking period by default
        active_mps = course.marking_period.filter(active=True)
        if active_mps:
            filter_form = GradebookFilterForm(initial={'marking_period': active_mps[0]})
            items = items.filter(marking_period=active_mps[0])
        else:
            filter_form = GradebookFilterForm()
        filter_form.update_querysets(course)
        
    # make a note of any aggregates pending recalculation
    pending_aggregate_pks = Aggregate.objects.filter(course=course).annotate(Count('aggregatetask')).filter(
                            aggregatetask__count__gt=0).values_list('pk', flat=True)
    
    # Freeze these now in case someone else gets in here!
    # TODO: something that actually works. all() does not evaluate a QuerySet.
    # https://docs.djangoproject.com/en/dev/ref/models/querysets/#when-querysets-are-evaluated
    items = items.order_by('id').all()
    # whoa, super roll of the dice. is Item.demonstration_set really guaranteed to be ordered by id?
    # precarious; sorting must match items (and demonstrations!) exactly
    marks = Mark.objects.filter(item__in=items).order_by('item__id', 'demonstration__id').all() 
    items_count = items.filter(demonstration=None).count() + Demonstration.objects.filter(item__in=items).count()
    for student in students:
        student_marks = marks.filter(student=student)
        if student_marks.count() < items_count:
            # maybe student enrolled after assignments were created
            for item in items:
                if len(item.demonstration_set.all()):
                    # must create mark for each demonstration
                    for demonstration in item.demonstration_set.all():
                        mark, created = Mark.objects.get_or_create(item=item, demonstration=demonstration, student=student)
                        if created:
                            mark.save()
                else:
                    # a regular item without demonstrations; make only one mark
                    mark, created = Mark.objects.get_or_create(item=item, student=student)
                    if created:
                        mark.save()
        if student_marks.count() > items_count:
            # Yikes, there are multiple marks per student per item. Stop loading the gradebook now.
            if 'dangerous' in request.GET:
                pass
            else:
                raise Exception('Multiple marks per student per item.')
        student.marks = student_marks
        average_tuple = gradebook_get_average_and_pk(student, course, None, None, None)
        student.average = average_tuple[0]
        student.average_pk = average_tuple[1]
        if filtered:
            student.filtered_average = gradebook_get_average(student, course, filter_form.cleaned_data['category'],
                                                             filter_form.cleaned_data['marking_period'], items)
        # TC's column of counts
        # TODO: don't hardcode
        standards_category = Category.objects.get(name='Standards')
        PASSING_GRADE = 3
        standards_objects = Item.objects.filter(course=course, category=standards_category, mark__student=student).annotate(best_mark=Max('mark__mark')).exclude(best_mark=None)
        standards_count_passing = standards_objects.filter(best_mark__gte=PASSING_GRADE).count()
        standards_count_total = standards_objects.count()
        if standards_count_total:
            student.standards_counts = '{} / {} ({:.0f}%)'.format(standards_count_passing, standards_count_total, 100.0 * standards_count_passing / standards_count_total)
        else:
            student.standards_counts_ = None
        if filtered:
            standards_objects = items.filter(course=course, category=standards_category, mark__student=student).annotate(best_mark=Max('mark__mark')).exclude(best_mark=None)
            standards_count_passing = standards_objects.filter(best_mark__gte=PASSING_GRADE).count()
            standards_count_total = standards_objects.count()
            if standards_count_total:
                student.filtered_standards_counts = '{} / {} ({:.0f}%)'.format(standards_count_passing, standards_count_total, 100.0 * standards_count_passing / standards_count_total)
            else:
                student.filtered_standards_counts = None

        # TC's row of counts
        # TODO: don't hardcode
        for item in items:
            if item.category != standards_category:
                item.marks_counts = 'N/A'
                continue
            marks_count_passing = item.mark_set.filter(mark__gte=PASSING_GRADE).count()
            marks_count_total = item.mark_set.exclude(mark=None).count()
            if marks_count_total:
                item.marks_counts = '{} / {} ({:.0f}%)'.format(marks_count_passing, marks_count_total, 100.0 * marks_count_passing / marks_count_total)
            else:
                item.marks_counts = None

    # Gather visual flagging criteria
    calculation_rule = benchmark_find_calculation_rule(course.marking_period.all()[0].school_year)
    category_flag_criteria = {}
    for category in Category.objects.filter(item__in=items).distinct():
        category_flag_criteria[category.pk] = []
        substitutions = calculation_rule.substitution_set.filter(apply_to_departments=course.department, apply_to_categories=category, flag_visually=True)
        for substitution in substitutions:
            category_flag_criteria[category.pk].append(substitution.operator + ' ' + str(substitution.match_value))

    return render_to_response('benchmark_grade/gradebook.html', {
        'items': items,
        'item_pks': ','.join(map(str,items.values_list('pk', flat=True))),
        'pending_aggregate_pks': json.dumps(map(str, pending_aggregate_pks)),
        'students': students,
        'course': course,
        'teacher_courses': teacher_courses,
        'filtered' : filtered,
        'filter_form': filter_form,
        'category_flag_criteria': category_flag_criteria,
    }, RequestContext(request, {}),)

@staff_member_required
@transaction.commit_on_success
def ajax_delete_item_form(request, course_id, item_id):
    item = get_object_or_404(Item, pk=item_id)
    ghost_item = Item()
    ghost_item.course = item.course
    ghost_item.category = item.category
    ghost_item.marking_period = item.marking_period
    message = '%s deleted' % (item,)
    item.delete()
    gradebook_recalculate_on_item_change(ghost_item)
    messages.success(request, message)
    return HttpResponse('SUCCESS')

@staff_member_required
@transaction.commit_on_success
def ajax_get_item_form(request, course_id, item_id=None):
    ''' the transaction decorator helps, but people can still hammer the submit button
    and create tons of assignments. for some reason, only one shows up right away, and the rest
    don't appear until reload '''
    course = get_object_or_404(Course, pk=course_id)
    item = None
    lists = None
    
    if request.POST:
        if item_id:
            item = get_object_or_404(Item, pk=item_id)
            form = ItemForm(request.POST, instance=item, prefix="item")
        else:
            form = ItemForm(request.POST, prefix="item")
        if form.is_valid():
            if item_id is None:
                # a new item!
                item = form.save()
                dem = None
                if item.category.allow_multiple_demonstrations:
                    # must have at least one demonstration; create a new one
                    dem = Demonstration()
                    dem.name = 'Dem. 1'
                    dem.item = item
                    dem.save()
                # must create blank marks for each student
                for student in Student.objects.filter(course=course):
                    mark, created = Mark.objects.get_or_create(item=item, student=student, demonstration=dem)
                    if created:
                        mark.save()
            else:
                # modifying an existing item
                old_item = Item.objects.get(pk=item.pk)
                item = form.save()
                gradebook_recalculate_on_item_change(item, old_item=old_item)

            # Should I use the django message framework to inform the user?
            # This would not work in ajax unless we make some sort of ajax
            # message handler.
            messages.success(request, '%s saved' % (item,))
            return HttpResponse('SUCCESS')
        
    else:
        if item_id:
            item = get_object_or_404(Item, pk=item_id)
            form = ItemForm(instance=item, prefix="item")
            # TODO: remove TC hard-coding
            if item.category.name == 'Standards':
                students_missing = Student.objects.filter(mark__item=item).annotate(best_mark=Max('mark__mark')).filter(best_mark__lt=3)
                if not students_missing: students_missing = ('None',)
                lists = ({'heading':'Students Missing This Item', 'items':students_missing},)
        else:
            active_mps = course.marking_period.filter(active=True)
            if active_mps:
                form = ItemForm(initial={'course': course, 'marking_period':active_mps[0]}, prefix="item")
            else:
                form = ItemForm(initial={'course': course}, prefix="item")
    
    form.fields['marking_period'].queryset = course.marking_period.all()
    form.fields['category'].queryset = Category.objects.filter(display_in_gradebook=True)
    form.fields['benchmark'].queryset = Benchmark.objects.filter()

    form.fields['category'].widget.attrs = {
        'onchange': "Dajaxice.ecwsp.benchmark_grade.check_fixed_points_possible(Dajax.process, {'category':this.value})"}
    if item and item.category.fixed_points_possible:
        form.fields['points_possible'].widget.attrs = {'disabled': 'true'}

    return render_to_response('sis/generic_form_fragment.html', {
        'form': form,
        'item_id': item_id,
        'lists': lists,
    }, RequestContext(request, {}),)

@staff_member_required
def ajax_get_item_tooltip(request, course_id, item_id):
    course = get_object_or_404(Course, pk=course_id)
    item = get_object_or_404(Item, pk=item_id)
    attribute_names = (
        'name',
        'description',
        'date',
        'marking_period',
        'category',
        'points_possible',
        'assignment_type',
        'benchmark',
    )
    verbose_name_overrides = {
        'benchmark': 'standard',
    }
    details = {}
    for a in attribute_names:
        if a in verbose_name_overrides:
            verbose_name = verbose_name_overrides[a]
        else:
            verbose_name = item._meta.get_field(a).verbose_name
        value = getattr(item, a)
        details[verbose_name] = value
    return render_to_response('benchmark_grade/item_details.html', {
        'details': details,
    }, RequestContext(request, {}),)

@staff_member_required
@transaction.commit_on_success
def ajax_delete_demonstration_form(request, course_id, demonstration_id):
    demonstration = get_object_or_404(Demonstration, pk=demonstration_id)
    item = demonstration.item
    ghost_item = Item()
    ghost_item.course = item.course
    ghost_item.category = item.category
    ghost_item.marking_period = item.marking_period
    message = '%s deleted' % (demonstration,)
    demonstration.delete()
    if not Demonstration.objects.filter(item=item):
        if Mark.objects.filter(item=item):
            raise Exception('Stray marks found after attempting to delete last demonstration.')
        else:
            # the last demonstration is dead. kill the item.
            item.delete()

    gradebook_recalculate_on_item_change(ghost_item)
    messages.success(request, message)
    return HttpResponse('SUCCESS')

@staff_member_required
@transaction.commit_on_success
def ajax_get_demonstration_form(request, course_id, demonstration_id=None):
    ''' the transaction decorator helps, but people can still hammer the submit button
    and create tons of assignments. for some reason, only one shows up right away, and the rest
    don't appear until reload '''
    course = get_object_or_404(Course, pk=course_id)
    lists = None
    
    if request.POST:
        if demonstration_id:
            demonstration = get_object_or_404(Demonstration, pk=demonstration_id)
            form = DemonstrationForm(request.POST, instance=demonstration, prefix="demonstration")
        else:
            form = DemonstrationForm(request.POST, prefix="demonstration")
        if form.is_valid():
            demonstration = form.save()
            if demonstration_id is None:
                # a new demonstration; must create blank marks for each student
                for student in Student.objects.filter(course=course):
                    mark, created = Mark.objects.get_or_create(item=demonstration.item, demonstration=demonstration, student=student)
                    if created:
                        mark.save()

            # Should I use the django message framework to inform the user?
            # This would not work in ajax unless we make some sort of ajax
            # message handler.
            messages.success(request, '%s saved' % (demonstration,))
            return HttpResponse('SUCCESS')
        
    else:
        if demonstration_id:
            demonstration = get_object_or_404(Demonstration, pk=demonstration_id)
            form = DemonstrationForm(instance=demonstration, prefix="demonstration")
            # TODO: remove TC hard-coding
            if demonstration.item.category.name == 'Standards':
                students_missing = Student.objects.filter(mark__demonstration=demonstration, mark__mark__lt=3)
                if not students_missing: students_missing = ('None',)
                lists = ({'heading':'Students Missing This Demonstration', 'items':students_missing},)
        else:
            form = DemonstrationForm(initial={'course': course}, prefix="demonstration")
    
    form.fields['item'].queryset = Item.objects.filter(course=course,
                                                       category__display_in_gradebook=True, category__allow_multiple_demonstrations=True)

    return render_to_response('benchmark_grade/demonstration_form_fragment.html', {
        'form': form,
        'demonstration_id': demonstration_id,
        'lists': lists,
    }, RequestContext(request, {}),)

@staff_member_required
def ajax_get_student_info(request, course_id, student_id):
    student = get_object_or_404(Student, pk=student_id)
    course = get_object_or_404(Course, pk=course_id)

    # TODO: remove TC hard-coding
    standards_missing = Item.objects.filter(course=course, category__name='Standards', mark__student=student).annotate(best_mark=Max('mark__mark')).filter(best_mark__lt=3)
    if not standards_missing: standards_missing = ('None',)
    lists = ({'heading':'Standards Missing for {}'.format(student), 'items':standards_missing},)
    afterword = '<a onclick="open_grade_detail({}, {})">Create report from current view of gradebook (in new tab)</a>'
    afterword = afterword.format(course_id, student_id)

    return render_to_response('sis/generic_list_fragment.html', {
        'lists': lists,
        'afterword': afterword,
    }, RequestContext(request, {}),)

@staff_member_required
def ajax_get_fill_all_form(request, course_id, object_type, object_id):
    model_base = Item if object_type == 'item' else Demonstration
    item_or_demonstration = get_object_or_404(model_base, pk=object_id)
    course = get_object_or_404(Course, pk=course_id)
    if type(item_or_demonstration) == Item and item_or_demonstration.course != course:
        raise Exception('This Item does not belong to the specified Course.')
    if type(item_or_demonstration) == Demonstration and item_or_demonstration.item.course != course:
        raise Exception('This Demonstration does not belong to the specified Course.')
    if type(item_or_demonstration) == Item and item_or_demonstration.category.allow_multiple_demonstrations:
        raise Exception('Marks must be assigned to Demonstrations for this Item, not directly to the Item.')
    if not item_or_demonstration.mark_set.count:
        raise Exception('This {} has no Marks.'.format(item_or_demonstration._meta.object_name))

    if request.POST:
        form = FillAllForm(request.POST, prefix="fill_all")
        if form.is_valid():
            for m in item_or_demonstration.mark_set.all():
                m.mark = form.cleaned_data['mark']
                m.save()
            messages.success(request, 'Marked all students {} for {}'.format(form.cleaned_data['mark'], item_or_demonstration))
            return HttpResponse('SUCCESS')
    else:
        form = FillAllForm(instance=item_or_demonstration.mark_set.all()[0], prefix="fill_all")
    return render_to_response('benchmark_grade/fill_all_form_fragment.html', {
        'action': request.path,
        'form': form,
        'subtitle': unicode(item_or_demonstration),
    }, RequestContext(request, {}),)

@staff_member_required
def ajax_save_grade(request):
    if 'mark_id' in request.POST and 'value' in request.POST:
        mark_id = request.POST['mark_id'].strip()
        value = request.POST['value'].strip()
        try: mark = Mark.objects.get(id=mark_id)
        except Mark.DoesNotExist: return HttpResponse('NO MARK WITH ID ' + mark_id, status=404) 
        if not request.user.is_superuser and not request.user.groups.filter(name='registrar').count() \
            and request.user.username != mark.item.course.teacher.username \
            and not mark.item.course.secondary_teachers.filter(username=request.user.username).count():
            return HttpResponse(status=403)

        if len(value) and value.lower != 'none':
            mark.mark = value
        else:
            mark.mark = None
            value = 'None'
        # temporarily log who's changing stuff since i'll have to manually recalculate averages later
        mark.description += ',' + request.user.username
        try:
            mark.full_clean()
            mark.save()
        except Exception as e:
            return HttpResponse(e, status=400)
        affected_agg_pks = [x.pk for x in gradebook_recalculate_on_mark_change(mark)]
        # just the whole course average for now
        # TODO: update filtered average
        #average = gradebook_get_average(mark.student, mark.item.course, None, None, None) 
        return HttpResponse(json.dumps({'success': 'SUCCESS', 'value': value, 'average': 'Please clear your browser\'s cache.', 'affected_aggregates': affected_agg_pks}))
    else:
        return HttpResponse('POST DATA INCOMPLETE', status=400) 

@staff_member_required
def ajax_task_poll(request, course_pk=None):
    if 'aggregate_pks[]' not in request.POST:
        # no aggregates specified; just return the number of active tasks for this course
        course = get_object_or_404(Course, pk=course_pk)
        count = AggregateTask.objects.values('task_id').distinct().count()
        return HttpResponse(json.dumps({'outstanding_tasks': count}))
    agg_pks = request.POST.getlist('aggregate_pks[]')
    aggs = Aggregate.objects.filter(pk__in=agg_pks)
    count = AggregateTask.objects.filter(aggregate__in=aggs).values('task_id').distinct().count()
    if count:
        # thank you, come again
        return HttpResponse(json.dumps({'outstanding_tasks': count}), status=202)
    else:
        # no outstanding tasks! return actual values!
        results = {}
        for agg in aggs:
            if agg.cached_substitution is not None:
                results[agg.pk] = str(agg.cached_substitution)
            else:
                results[agg.pk] = str(agg.cached_value)
        return HttpResponse(json.dumps({'results': results}))

@login_required
def student_report(request, student_pk=None, course_pk=None, marking_period_pk=None):
    authorized = False
    family_available_students = None
    try:
        # is it a student?
        student = Student.objects.get(username=request.user.username)
        # ok! we'll ignore student_pk, and the student is authorized to see itself
        authorized = True
    except:
        student = None
    if not student:
        if request.user.is_staff:
            # hey, it's a staff member!
            student = get_object_or_404(Student, pk=student_pk)
            authorized = True
        else:
            # maybe it's a family member?
            family_available_students = Student.objects.filter(family_access_users=request.user)
            if student_pk:
                student = get_object_or_404(Student, pk=student_pk)
                if student in family_available_students:
                    authorized = True
            elif family_available_students.count():
                student = family_available_students[0]
                authorized = True
    
    # did all that make us comfortable with proceeding?
    if not authorized:
        error_message = 'Sorry, you are not authorized to see grades for this student. Please contact the school registrar.'
        return render_to_response('benchmark_grade/student_grade.html', {
            'error_message': error_message,
        }, RequestContext(request, {}),)

    # is this a summary or detail report?
    if not course_pk:
        # summary report for all courses
        PASSING_GRADE = 3 # TODO: pull config value. Roche has it set to something crazy now and I don't want to deal with it
        school_year = SchoolYear.objects.get(active_year=True)
        mps = MarkingPeriod.objects.filter(school_year=school_year, start_date__lte=datetime.date.today()).order_by('-start_date')
        calculation_rule = benchmark_find_calculation_rule(school_year)
        for mp in mps:
            mp.courses = Course.objects.filter(courseenrollment__user=student, graded=True, marking_period=mp).order_by('fullname')
            for course in mp.courses:
                course.categories = Category.objects.filter(item__course=course, item__mark__student=student).distinct()
                course.category_by_name = {}
                for category in course.categories:
                    category.percentage = calculation_rule.per_course_category_set.get(
                        category=category, apply_to_departments=course.department).weight * 100
                    category.percentage = category.percentage.quantize(Decimal('0'))
                    category.average = gradebook_get_average(student, course, category, mp, None)
                    items = Item.objects.filter(course=course, category=category, marking_period=mp, mark__student=student).annotate(best_mark=Max('mark__mark'))
                    counts = {}
                    counts['total'] = items.exclude(best_mark=None).distinct().count()
                    counts['missing'] = items.filter(best_mark__lt=PASSING_GRADE).distinct().count()
                    counts['passing'] = items.filter(best_mark__gte=PASSING_GRADE).distinct().count()
                    if counts['total']:
                        counts['percentage'] = (Decimal(counts['passing']) / counts['total'] * 100).quantize(Decimal('0'))
                    course.category_by_name[category.name] = counts
                course.average = gradebook_get_average(student, course, None, mp, None)

        return render_to_response('benchmark_grade/student_grade.html', {
            'student': student,
            'available_students': family_available_students,
            'mps': mps
        }, RequestContext(request, {}),)

    else:
        # detail report for a single course
        course = get_object_or_404(Course, pk=course_pk)

        # TODO: move into CalculationRule?
        CATEGORY_NAME_TO_FLAG_CRITERIA = {
            'Standards': {'best_mark__lt': 3},
            'Engagement': {'best_mark__lt': 3},
            'Organization': {'best_mark__lt': 3},
            'Daily Practice': {'best_mark__lte': 0},
        }

        if 'item_pks' in request.POST:
            item_pks = request.POST['item_pks'].split(',')
            items = Item.objects.filter(pk__in=item_pks)
            specific_items = True
        else:
            items = Item.objects
            specific_items = False
        # always filter in case a bad person passes us items from a different course
        items = items.filter(course=course, mark__student=student)

        if marking_period_pk:
            mp = get_object_or_404(MarkingPeriod, pk=marking_period_pk)
            mps = (mp,)
        else:
            mps = MarkingPeriod.objects.filter(item__in=items).distinct().order_by('-start_date')

        for mp in mps:
            mp_items = items.filter(marking_period=mp)
            mp.categories = Category.objects.filter(item__in=mp_items).distinct()
            for category in mp.categories:
                category_items = mp_items.filter(category=category).annotate(best_mark=Max('mark__mark')).exclude(best_mark=None)
                item_names = category_items.values_list('name').distinct()
                category.item_groups = {}
                for item_name_tuple in item_names:
                    item_name = item_name_tuple[0]
                    category.item_groups[item_name] = category_items.filter(name=item_name).distinct() 
                if specific_items:
                    # get a disposable average for these specific items
                    category.average = gradebook_get_average(student, course, category, mp, category_items)
                else:
                    category.average = gradebook_get_average(student, course, category, mp, None)
                category.flagged_item_pks = []
                if category.name in CATEGORY_NAME_TO_FLAG_CRITERIA:
                    category.flagged_item_pks = category_items.filter(**CATEGORY_NAME_TO_FLAG_CRITERIA[category.name]).values_list('pk', flat=True)

        return render_to_response('benchmark_grade/student_grade_course_detail.html', {
            'student': student,
            'course': course,
            'mps': mps
        }, RequestContext(request, {}),)
